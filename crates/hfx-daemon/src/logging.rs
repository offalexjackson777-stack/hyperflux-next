// SPDX-License-Identifier: GPL-2.0-only

use hfx_core::{EventDelivery, EventSink};
use hfx_protocol::BridgeEvent;
use std::fmt;
use std::io::{self, Write};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, SyncSender, TrySendError, sync_channel};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StructuredEventLoggerError {
    InvalidCapacity,
    SpawnFailed,
    OutputFailed,
    WorkerPanicked,
    ShutdownTimedOut,
}

impl fmt::Display for StructuredEventLoggerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InvalidCapacity => "structured event queue capacity is invalid",
            Self::SpawnFailed => "structured event logger could not start",
            Self::OutputFailed => "structured event logger output failed",
            Self::WorkerPanicked => "structured event logger stopped unexpectedly",
            Self::ShutdownTimedOut => "structured event logger shutdown exceeded its bound",
        })
    }
}

impl std::error::Error for StructuredEventLoggerError {}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StructuredEventLoggerExit {
    pub emitted: u64,
}

/// Nonblocking bridge event sink backed by one bounded logger queue.
#[derive(Clone, Debug)]
pub struct StructuredEventSink {
    sender: SyncSender<BridgeEvent>,
    dropped: Arc<AtomicU64>,
}

impl StructuredEventSink {
    /// Starts one named worker that writes newline-delimited JSON to stderr.
    ///
    /// # Errors
    ///
    /// Rejects a zero capacity or a worker spawn failure.
    pub fn spawn_stderr(
        capacity: usize,
    ) -> Result<(Self, StructuredEventLogger), StructuredEventLoggerError> {
        let (sink, receiver) = Self::channel(capacity)?;
        let (completed_sender, completed) = sync_channel(1);
        let worker = thread::Builder::new()
            .name("hfx-event-log".to_owned())
            .spawn(move || {
                let stderr = io::stderr();
                let result = drain_events(&receiver, stderr.lock()).map(|(exit, _)| exit);
                let _ = completed_sender.send(result);
                result
            })
            .map_err(|_| StructuredEventLoggerError::SpawnFailed)?;
        Ok((sink, StructuredEventLogger { worker, completed }))
    }

    #[must_use]
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }

    fn channel(
        capacity: usize,
    ) -> Result<(Self, Receiver<BridgeEvent>), StructuredEventLoggerError> {
        if capacity == 0 {
            return Err(StructuredEventLoggerError::InvalidCapacity);
        }
        let (sender, receiver) = sync_channel(capacity);
        Ok((
            Self {
                sender,
                dropped: Arc::new(AtomicU64::new(0)),
            },
            receiver,
        ))
    }
}

impl EventSink for StructuredEventSink {
    fn try_emit(&mut self, event: &BridgeEvent) -> EventDelivery {
        match self.sender.try_send(event.clone()) {
            Ok(()) => EventDelivery::Accepted,
            Err(TrySendError::Full(_)) => {
                self.dropped.fetch_add(1, Ordering::Relaxed);
                EventDelivery::Full
            }
            Err(TrySendError::Disconnected(_)) => EventDelivery::Closed,
        }
    }
}

pub struct StructuredEventLogger {
    worker: JoinHandle<Result<StructuredEventLoggerExit, StructuredEventLoggerError>>,
    completed: Receiver<Result<StructuredEventLoggerExit, StructuredEventLoggerError>>,
}

impl StructuredEventLogger {
    /// Waits for the logger after all sink handles have been dropped.
    ///
    /// # Errors
    ///
    /// Returns a typed output or worker failure.
    pub fn join(self) -> Result<StructuredEventLoggerExit, StructuredEventLoggerError> {
        self.worker
            .join()
            .map_err(|_| StructuredEventLoggerError::WorkerPanicked)?
    }

    /// Waits only for the declared shutdown bound. A blocked output worker is
    /// detached so service termination never waits forever for an unread log
    /// destination.
    ///
    /// # Errors
    ///
    /// Returns the worker result, a panic, or a bounded shutdown timeout.
    pub fn finish(
        self,
        timeout: Duration,
    ) -> Result<StructuredEventLoggerExit, StructuredEventLoggerError> {
        let Self { worker, completed } = self;
        match completed.recv_timeout(timeout) {
            Ok(expected) => {
                let actual = worker
                    .join()
                    .map_err(|_| StructuredEventLoggerError::WorkerPanicked)?;
                if actual == expected {
                    actual
                } else {
                    Err(StructuredEventLoggerError::WorkerPanicked)
                }
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                drop(worker);
                Err(StructuredEventLoggerError::ShutdownTimedOut)
            }
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => worker
                .join()
                .map_err(|_| StructuredEventLoggerError::WorkerPanicked)?,
        }
    }
}

fn drain_events<W: Write>(
    receiver: &Receiver<BridgeEvent>,
    mut writer: W,
) -> Result<(StructuredEventLoggerExit, W), StructuredEventLoggerError> {
    let mut emitted = 0_u64;
    while let Ok(event) = receiver.recv() {
        serde_json::to_writer(&mut writer, &event)
            .map_err(|_| StructuredEventLoggerError::OutputFailed)?;
        writer
            .write_all(b"\n")
            .and_then(|()| writer.flush())
            .map_err(|_| StructuredEventLoggerError::OutputFailed)?;
        emitted = emitted.saturating_add(1);
    }
    Ok((StructuredEventLoggerExit { emitted }, writer))
}

#[cfg(test)]
mod tests {
    use super::*;
    use hfx_domain::{EventKind, SequenceNumber};

    fn event(sequence: u64) -> BridgeEvent {
        BridgeEvent {
            sequence: SequenceNumber::try_from(sequence).expect("sequence is valid"),
            kind: EventKind::ReceiverAvailable,
            receiver_id: None,
            generation_id: None,
            device_id: None,
            lease_id: None,
            transaction_id: None,
            finding_id: None,
        }
    }

    #[test]
    fn producer_never_waits_when_queue_is_full_or_closed() {
        let (mut sink, receiver) = StructuredEventSink::channel(1).expect("channel creates");
        assert_eq!(sink.try_emit(&event(1)), EventDelivery::Accepted);
        assert_eq!(sink.try_emit(&event(2)), EventDelivery::Full);
        assert_eq!(sink.dropped(), 1);
        drop(receiver);
        assert_eq!(sink.try_emit(&event(3)), EventDelivery::Closed);
    }

    #[test]
    fn worker_emits_one_valid_json_object_per_line() {
        let (mut sink, receiver) = StructuredEventSink::channel(2).expect("channel creates");
        assert_eq!(sink.try_emit(&event(7)), EventDelivery::Accepted);
        drop(sink);
        let (exit, output) = drain_events(&receiver, Vec::new()).expect("events drain");
        assert_eq!(exit.emitted, 1);
        let line = String::from_utf8(output).expect("output is utf-8");
        assert_eq!(line.lines().count(), 1);
        let decoded: BridgeEvent = serde_json::from_str(line.trim()).expect("event decodes");
        assert_eq!(decoded, event(7));
    }

    #[test]
    fn bounded_finish_never_waits_for_a_stuck_worker() {
        let (_sender, completed) = sync_channel(1);
        let worker = thread::spawn(|| {
            thread::sleep(Duration::from_millis(100));
            Ok(StructuredEventLoggerExit { emitted: 0 })
        });
        let logger = StructuredEventLogger { worker, completed };
        let started = std::time::Instant::now();
        assert_eq!(
            logger.finish(Duration::from_millis(1)),
            Err(StructuredEventLoggerError::ShutdownTimedOut)
        );
        assert!(started.elapsed() < Duration::from_millis(50));
    }
}
