// SPDX-License-Identifier: GPL-2.0-only

use crate::entropy::{EntropyUnavailable, fill_random};
use hfx_domain::{ProductId, ReceiverId, VendorId};
use rustix::fs::{CWD, Mode, OFlags, RenameFlags, open, renameat_with};
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;

const IDENTITY_SECRET_BYTES: usize = 32;
const LOWER_HEX: &[u8; 16] = b"0123456789abcdef";

#[derive(Debug)]
pub enum ReceiverIdentityError {
    EntropyUnavailable,
    InvalidSecretFile,
    Io,
    InvalidReceiverId,
}

impl fmt::Display for ReceiverIdentityError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::EntropyUnavailable => "receiver identity entropy is unavailable",
            Self::InvalidSecretFile => "receiver identity secret is invalid",
            Self::Io => "receiver identity storage failed",
            Self::InvalidReceiverId => "derived receiver identity is invalid",
        })
    }
}

impl std::error::Error for ReceiverIdentityError {}

#[derive(Clone)]
pub struct ReceiverIdentityAuthority {
    secret: [u8; IDENTITY_SECRET_BYTES],
}

impl ReceiverIdentityAuthority {
    /// Loads or creates one private installation-local identity secret.
    ///
    /// # Errors
    ///
    /// Rejects symlinks, non-private files, malformed content, unavailable
    /// entropy, and I/O failures without exposing the private path.
    pub fn load_or_create(path: &Path) -> Result<Self, ReceiverIdentityError> {
        match open(
            path,
            OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::empty(),
        ) {
            Ok(descriptor) => Self::read_existing(File::from(descriptor)),
            Err(rustix::io::Errno::NOENT) => Self::create(path),
            Err(_) => Err(ReceiverIdentityError::Io),
        }
    }

    #[must_use]
    pub const fn from_secret(secret: [u8; IDENTITY_SECRET_BYTES]) -> Self {
        Self { secret }
    }

    /// Derives a stable installation-local identifier without retaining or
    /// returning the raw host topology path.
    ///
    /// # Errors
    ///
    /// Returns a typed error only if the canonical domain value rejects the
    /// generated identifier.
    pub fn derive(
        &self,
        topology_path: &Path,
        vendor_id: VendorId,
        product_id: ProductId,
    ) -> Result<ReceiverId, ReceiverIdentityError> {
        let mut digest = Sha256::new();
        digest.update(b"hyperflux-receiver-id-v1\0");
        digest.update(self.secret);
        digest.update(topology_path.as_os_str().as_encoded_bytes());
        digest.update(vendor_id.get().to_be_bytes());
        digest.update(product_id.get().to_be_bytes());
        let bytes = digest.finalize();
        let mut value = String::from("receiver-");
        for byte in &bytes[..12] {
            value.push(char::from(LOWER_HEX[usize::from(byte >> 4)]));
            value.push(char::from(LOWER_HEX[usize::from(byte & 0x0f)]));
        }
        ReceiverId::try_from(value).map_err(|_| ReceiverIdentityError::InvalidReceiverId)
    }

    fn create(path: &Path) -> Result<Self, ReceiverIdentityError> {
        let mut secret = [0_u8; IDENTITY_SECRET_BYTES];
        fill_random(&mut secret)
            .map_err(|EntropyUnavailable| ReceiverIdentityError::EntropyUnavailable)?;
        let temporary = temporary_path(path)?;
        let descriptor = open(
            &temporary,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::RUSR | Mode::WUSR,
        )
        .map_err(|_| ReceiverIdentityError::Io)?;
        let mut file = File::from(descriptor);
        let publish = file
            .write_all(&secret)
            .and_then(|()| file.sync_all())
            .map_err(|_| ReceiverIdentityError::Io)
            .and_then(|()| {
                renameat_with(CWD, &temporary, CWD, path, RenameFlags::NOREPLACE).map_err(|error| {
                    match error {
                        rustix::io::Errno::EXIST => ReceiverIdentityError::InvalidSecretFile,
                        _ => ReceiverIdentityError::Io,
                    }
                })
            });
        drop(file);

        match publish {
            Ok(()) => {
                sync_parent(path)?;
                Ok(Self { secret })
            }
            Err(ReceiverIdentityError::InvalidSecretFile) => {
                let _ = fs::remove_file(&temporary);
                sync_parent(path)?;
                let descriptor = open(
                    path,
                    OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
                    Mode::empty(),
                )
                .map_err(|_| ReceiverIdentityError::Io)?;
                Self::read_existing(File::from(descriptor))
            }
            Err(error) => {
                let _ = fs::remove_file(&temporary);
                Err(error)
            }
        }
    }

    fn read_existing(mut file: File) -> Result<Self, ReceiverIdentityError> {
        let metadata = file.metadata().map_err(|_| ReceiverIdentityError::Io)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::{MetadataExt, PermissionsExt};
            if !metadata.file_type().is_file()
                || metadata.len() != IDENTITY_SECRET_BYTES as u64
                || metadata.permissions().mode() & 0o077 != 0
                || metadata.nlink() != 1
            {
                return Err(ReceiverIdentityError::InvalidSecretFile);
            }
        }
        let mut secret = [0_u8; IDENTITY_SECRET_BYTES];
        file.read_exact(&mut secret)
            .map_err(|_| ReceiverIdentityError::InvalidSecretFile)?;
        let mut trailing = [0_u8; 1];
        if file
            .read(&mut trailing)
            .map_err(|_| ReceiverIdentityError::Io)?
            != 0
        {
            return Err(ReceiverIdentityError::InvalidSecretFile);
        }
        Ok(Self { secret })
    }
}

fn temporary_path(path: &Path) -> Result<std::path::PathBuf, ReceiverIdentityError> {
    let parent = path.parent().ok_or(ReceiverIdentityError::Io)?;
    let name = path.file_name().ok_or(ReceiverIdentityError::Io)?;
    let mut nonce = [0_u8; 8];
    fill_random(&mut nonce)
        .map_err(|EntropyUnavailable| ReceiverIdentityError::EntropyUnavailable)?;
    let mut suffix = String::with_capacity(nonce.len() * 2);
    for byte in nonce {
        suffix.push(char::from(LOWER_HEX[usize::from(byte >> 4)]));
        suffix.push(char::from(LOWER_HEX[usize::from(byte & 0x0f)]));
    }
    let mut temporary_name = OsString::from(".");
    temporary_name.push(name);
    temporary_name.push(format!(".tmp-{}-{suffix}", std::process::id()));
    Ok(parent.join(temporary_name))
}

fn sync_parent(path: &Path) -> Result<(), ReceiverIdentityError> {
    let parent = path.parent().ok_or(ReceiverIdentityError::Io)?;
    File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| ReceiverIdentityError::Io)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn typed<T>(value: u16) -> T
    where
        T: TryFrom<u16>,
        T::Error: fmt::Debug,
    {
        T::try_from(value).expect("test value is valid")
    }

    fn temporary_path() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock is after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("hfx-identity-{}-{nonce}", std::process::id()))
    }

    #[test]
    fn derived_identity_is_stable_local_and_topology_specific() {
        let first = ReceiverIdentityAuthority::from_secret([0x11; 32]);
        let second = ReceiverIdentityAuthority::from_secret([0x22; 32]);
        let vendor = typed::<VendorId>(0x1532);
        let product = typed::<ProductId>(0x00cf);

        let a = first
            .derive(Path::new("/sys/devices/pci/usb/3-2"), vendor, product)
            .expect("identity derives");
        assert_eq!(
            a,
            first
                .derive(Path::new("/sys/devices/pci/usb/3-2"), vendor, product)
                .expect("identity repeats")
        );
        assert_ne!(
            a,
            first
                .derive(Path::new("/sys/devices/pci/usb/3-3"), vendor, product)
                .expect("other topology derives")
        );
        assert_ne!(
            a,
            second
                .derive(Path::new("/sys/devices/pci/usb/3-2"), vendor, product)
                .expect("other installation derives")
        );
    }

    #[test]
    fn identity_secret_is_private_exact_and_reusable() {
        let directory = temporary_path();
        fs::create_dir(&directory).expect("temporary directory creates");
        let path = directory.join("identity-secret");
        let first = ReceiverIdentityAuthority::load_or_create(&path).expect("secret creates");
        let second = ReceiverIdentityAuthority::load_or_create(&path).expect("secret reloads");
        assert_eq!(
            fs::metadata(&path)
                .expect("metadata reads")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            first
                .derive(Path::new("/sys/test"), typed(1), typed(2))
                .expect("first derives"),
            second
                .derive(Path::new("/sys/test"), typed(1), typed(2))
                .expect("second derives")
        );
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }

    #[test]
    fn permissive_or_malformed_secret_is_rejected() {
        let directory = temporary_path();
        fs::create_dir(&directory).expect("temporary directory creates");
        let path = directory.join("identity-secret");
        fs::write(&path, [0x44; 31]).expect("malformed secret writes");
        assert!(matches!(
            ReceiverIdentityAuthority::load_or_create(&path),
            Err(ReceiverIdentityError::InvalidSecretFile)
        ));
        fs::write(&path, [0x44; 32]).expect("secret rewrites");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).expect("permissions change");
        assert!(matches!(
            ReceiverIdentityAuthority::load_or_create(&path),
            Err(ReceiverIdentityError::InvalidSecretFile)
        ));
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }

    #[test]
    fn concurrent_first_start_converges_on_one_secret() {
        let directory = temporary_path();
        fs::create_dir(&directory).expect("temporary directory creates");
        let path = std::sync::Arc::new(directory.join("identity-secret"));
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let mut workers = Vec::new();
        for _ in 0..8 {
            let path = std::sync::Arc::clone(&path);
            let barrier = std::sync::Arc::clone(&barrier);
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                ReceiverIdentityAuthority::load_or_create(path.as_ref())
                    .expect("concurrent authority initializes")
                    .derive(Path::new("/sys/test"), typed(1), typed(2))
                    .expect("identity derives")
            }));
        }
        let identities = workers
            .into_iter()
            .map(|worker| worker.join().expect("worker joins"))
            .collect::<Vec<_>>();
        assert!(identities.windows(2).all(|pair| pair[0] == pair[1]));
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }
}
