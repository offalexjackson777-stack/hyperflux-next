// SPDX-License-Identifier: GPL-2.0-only

use rustix::fs::{FlockOperation, Mode, OFlags, flock, open};
use std::fmt;
use std::fs::{self, File};
use std::net::Shutdown;
use std::os::unix::fs::{FileTypeExt as _, MetadataExt as _, PermissionsExt as _};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SocketBindError {
    InvalidParent,
    InvalidLock,
    AlreadyRunning,
    InvalidStaleEntry,
    Bind,
    Permission,
    GroupInheritance,
}

impl fmt::Display for SocketBindError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidParent => "bridge runtime directory authority is invalid",
            Self::InvalidLock => "bridge process lock authority is invalid",
            Self::AlreadyRunning => "another bridge process owns the runtime",
            Self::InvalidStaleEntry => "bridge socket path contains an untrusted entry",
            Self::Bind => "bridge SDK socket could not be bound",
            Self::Permission => "bridge SDK socket permissions could not be established",
            Self::GroupInheritance => "bridge SDK socket did not inherit client group authority",
        })
    }
}

impl std::error::Error for SocketBindError {}

pub struct BoundUnixListener {
    listener: UnixListener,
    _lock: File,
    socket_path: PathBuf,
    socket_device: u64,
    socket_inode: u64,
}

impl BoundUnixListener {
    /// Acquires the process lock and binds one group-scoped SDK socket.
    ///
    /// # Errors
    ///
    /// Rejects untrusted parents, locks, stale entries, concurrent service
    /// owners, incorrect modes, and incorrect group inheritance.
    pub fn bind(socket_path: &Path, lock_path: &Path) -> Result<Self, SocketBindError> {
        let parent = socket_path.parent().ok_or(SocketBindError::InvalidParent)?;
        if lock_path.parent() != Some(parent) || socket_path == lock_path {
            return Err(SocketBindError::InvalidLock);
        }
        let parent_metadata =
            fs::symlink_metadata(parent).map_err(|_| SocketBindError::InvalidParent)?;
        let current_uid = rustix::process::geteuid().as_raw();
        if !parent_metadata.file_type().is_dir()
            || parent_metadata.uid() != current_uid
            || parent_metadata.permissions().mode() & 0o022 != 0
        {
            return Err(SocketBindError::InvalidParent);
        }

        let lock_descriptor = open(
            lock_path,
            OFlags::RDWR | OFlags::CREATE | OFlags::CLOEXEC | OFlags::NOFOLLOW,
            Mode::RUSR | Mode::WUSR,
        )
        .map_err(|_| SocketBindError::InvalidLock)?;
        let lock = File::from(lock_descriptor);
        let lock_metadata = lock.metadata().map_err(|_| SocketBindError::InvalidLock)?;
        if !lock_metadata.file_type().is_file()
            || lock_metadata.uid() != current_uid
            || lock_metadata.nlink() != 1
            || lock_metadata.permissions().mode() & 0o777 != 0o600
        {
            return Err(SocketBindError::InvalidLock);
        }
        flock(&lock, FlockOperation::NonBlockingLockExclusive)
            .map_err(|_| SocketBindError::AlreadyRunning)?;

        remove_owned_stale_socket(socket_path, current_uid)?;
        let listener = UnixListener::bind(socket_path).map_err(|_| SocketBindError::Bind)?;
        if let Err(error) = fs::set_permissions(socket_path, fs::Permissions::from_mode(0o660)) {
            let _ = fs::remove_file(socket_path);
            let _ = error;
            return Err(SocketBindError::Permission);
        }
        let socket_metadata = fs::symlink_metadata(socket_path).map_err(|_| {
            let _ = fs::remove_file(socket_path);
            SocketBindError::Permission
        })?;
        if !socket_metadata.file_type().is_socket()
            || socket_metadata.uid() != current_uid
            || socket_metadata.gid() != parent_metadata.gid()
            || socket_metadata.permissions().mode() & 0o777 != 0o660
        {
            let _ = fs::remove_file(socket_path);
            return Err(SocketBindError::GroupInheritance);
        }
        Ok(Self {
            listener,
            _lock: lock,
            socket_path: socket_path.to_path_buf(),
            socket_device: socket_metadata.dev(),
            socket_inode: socket_metadata.ino(),
        })
    }

    #[must_use]
    pub const fn listener(&self) -> &UnixListener {
        &self.listener
    }

    pub fn shutdown_probe(&self) {
        if let Ok(stream) = UnixStream::connect(&self.socket_path) {
            let _ = stream.shutdown(Shutdown::Both);
        }
    }
}

impl Drop for BoundUnixListener {
    fn drop(&mut self) {
        let Ok(metadata) = fs::symlink_metadata(&self.socket_path) else {
            return;
        };
        if metadata.file_type().is_socket()
            && metadata.dev() == self.socket_device
            && metadata.ino() == self.socket_inode
        {
            let _ = fs::remove_file(&self.socket_path);
        }
    }
}

fn remove_owned_stale_socket(path: &Path, current_uid: u32) -> Result<(), SocketBindError> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err(SocketBindError::InvalidStaleEntry),
    };
    if !metadata.file_type().is_socket() || metadata.uid() != current_uid || metadata.nlink() != 1 {
        return Err(SocketBindError::InvalidStaleEntry);
    }
    fs::remove_file(path).map_err(|_| SocketBindError::InvalidStaleEntry)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temporary_directory() -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "hfx-production-socket-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("temporary directory creates");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o2750)).expect("runtime mode sets");
        path
    }

    #[test]
    fn exact_socket_is_private_locked_and_removed_on_drop() {
        let directory = temporary_directory();
        let socket = directory.join("bridge.sock");
        let lock = directory.join("bridge.lock");
        let listener = BoundUnixListener::bind(&socket, &lock).expect("listener binds");
        let metadata = fs::symlink_metadata(&socket).expect("socket metadata reads");
        assert_eq!(metadata.permissions().mode() & 0o777, 0o660);
        assert_eq!(
            BoundUnixListener::bind(&socket, &lock).err(),
            Some(SocketBindError::AlreadyRunning)
        );
        drop(listener);
        assert!(!socket.exists());
        assert!(lock.is_file());
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }

    #[test]
    fn stale_owned_socket_is_replaced_but_files_and_symlinks_are_rejected() {
        let directory = temporary_directory();
        let socket = directory.join("bridge.sock");
        let lock = directory.join("bridge.lock");
        drop(UnixListener::bind(&socket).expect("stale socket binds"));
        let listener = BoundUnixListener::bind(&socket, &lock).expect("stale socket replaces");
        drop(listener);

        fs::write(&socket, b"not a socket").expect("regular file writes");
        assert_eq!(
            BoundUnixListener::bind(&socket, &lock).err(),
            Some(SocketBindError::InvalidStaleEntry)
        );
        fs::remove_file(&socket).expect("regular file removes");
        let target = directory.join("target");
        fs::write(&target, b"target").expect("target writes");
        symlink(&target, &socket).expect("symlink creates");
        assert_eq!(
            BoundUnixListener::bind(&socket, &lock).err(),
            Some(SocketBindError::InvalidStaleEntry)
        );
        fs::remove_dir_all(directory).expect("temporary directory removes");
    }
}
