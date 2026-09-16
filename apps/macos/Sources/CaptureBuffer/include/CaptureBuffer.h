#ifndef COLE_CAPTURE_BUFFER_H
#define COLE_CAPTURE_BUFFER_H

#include <AudioToolbox/AudioToolbox.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* @canonical native_capture_callback -- the only AudioQueue PCM callback.
 * Exactly one AudioQueue producer and one writer consumer own each context.
 * Create/configure/free belong to the control executor, never the callback.
 * The writer must acknowledge frames only after successful actual file writes.
 * The context must outlive synchronous AudioQueueStop/AudioQueueDispose and all
 * callbacks; mark_queue_disposed certifies that external lifetime guarantee.
 * Stop does not free memory and pending Stop-drain callbacks retain their PCM.
 * After disposal/failure/maximum, late callbacks while context remains owned are
 * counted and ignored. Calling any method after a successful free is invalid.
 */

enum {
    CVL_CAPTURE_SAMPLE_RATE = 48000,
    CVL_CAPTURE_CHANNELS = 1,
    CVL_CAPTURE_FRAME_BYTES = 3,
    CVL_CAPTURE_BITS_PER_CHANNEL = 24,
    CVL_CAPTURE_DEFAULT_CALLBACK_FRAMES = 4096,
    CVL_CAPTURE_DEFAULT_RAW_CAPACITY_FRAMES = 262144,
    CVL_CAPTURE_DEFAULT_TELEMETRY_CAPACITY = 128,
    CVL_CAPTURE_MAX_RAW_CAPACITY_FRAMES = 1048576,
    CVL_CAPTURE_MAX_TELEMETRY_CAPACITY = 4096,
    CVL_CAPTURE_MAXIMUM_FRAMES = 28800000
};

typedef enum {
    CVL_CAPTURE_OK = 0,
    CVL_CAPTURE_INVALID_CONFIGURATION = 1,
    CVL_CAPTURE_ALLOCATION_FAILED = 2,
    CVL_CAPTURE_NON_LOCK_FREE_ATOMICS = 3,
    CVL_CAPTURE_INVALID_BUFFER = 4,
    CVL_CAPTURE_PARTIAL_PCM_FRAME = 5,
    CVL_CAPTURE_INVALID_TIMESTAMP = 6,
    CVL_CAPTURE_TIMESTAMP_DISCONTINUITY = 7,
    CVL_CAPTURE_RAW_OVERFLOW = 8,
    CVL_CAPTURE_CONCURRENT_PRODUCER = 9,
    CVL_CAPTURE_ENQUEUE_FAILED = 10,
    CVL_CAPTURE_INVALID_WRITE_ACKNOWLEDGMENT = 11,
    CVL_CAPTURE_DEVICE_CHANGED = 12,
    CVL_CAPTURE_FORMAT_CHANGED = 13,
    CVL_CAPTURE_WRITER_FAILED = 14,
    CVL_CAPTURE_PARENT_LOST = 15
} CVLCaptureError;

typedef struct {
    uint32_t sample_rate_hz;
    uint32_t channel_count;
    uint32_t bits_per_channel;
    uint32_t bytes_per_frame;
    uint32_t max_callback_frames;
    uint32_t raw_capacity_frames;
    uint32_t telemetry_capacity;
    uint64_t maximum_frames;
    uint64_t generation;
} CVLCaptureConfiguration;

typedef struct CVLCaptureBuffer CVLCaptureBuffer;

typedef struct {
    uint64_t generation;
    uint64_t start_frame;
    uint32_t frame_count;
    int32_t minimum_pcm;
    int32_t maximum_pcm;
    double sample_time;
    uint64_t host_time;
    bool host_time_valid;
} CVLCaptureEnvelope;

typedef struct {
    uint64_t generation;
    uint64_t received_frames; /* Accepted exact PCM; excludes explicitly reported limit tail. */
    uint64_t dequeued_frames; /* Handed to the writer, not evidence of successful writing. */
    uint64_t written_frames;  /* Independent writer acknowledgment; authoritative media clock. */
    uint64_t observed_frames;
    uint64_t limit_discarded_frames;
    uint64_t callback_count;
    uint64_t late_callback_count;
    uint64_t telemetry_dropped_count;
    uint32_t queued_frames;
    uint32_t active_callbacks;
    double first_sample_time;
    double next_sample_time;
    uint64_t first_host_time;
    bool first_host_time_valid;
    bool has_sample_time;
    bool stop_requested;
    bool maximum_reached;
    bool queue_disposed;
    CVLCaptureError error;
    int32_t enqueue_os_status;
} CVLCaptureSnapshot;

uint32_t cole_capture_buffer_abi_version(void);
CVLCaptureConfiguration cvl_capture_default_configuration(uint64_t generation);
CVLCaptureBuffer *cvl_capture_create(CVLCaptureConfiguration configuration,
                                    CVLCaptureError *error_out);
/* Returns false rather than freeing a live context. Call only after writer join
 * and successful synchronous AudioQueueDispose (or inert synthetic teardown).
 * No concurrent call may race this final release, including late callbacks.
 */
bool cvl_capture_free(CVLCaptureBuffer *buffer);
bool cvl_capture_format_is_canonical(const AudioStreamBasicDescription *format);
void cvl_capture_request_stop(CVLCaptureBuffer *buffer);
void cvl_capture_fail(CVLCaptureBuffer *buffer, CVLCaptureError error);
bool cvl_capture_mark_queue_disposed(CVLCaptureBuffer *buffer);
/* Synthetic entry point and AudioQueue callback share the identical processing
 * function; neither allocates, locks, writes files/pipes or invokes Swift.
 * timestamp_flags use Core Audio kAudioTimeStamp*Valid constants.
 * Empty buffers are permitted heartbeat callbacks and are never completed audio.
 */
bool cvl_capture_receive_pcm(CVLCaptureBuffer *buffer, const void *pcm,
                             uint32_t byte_count, uint32_t byte_capacity,
                             double sample_time, uint32_t timestamp_flags,
                             uint64_t host_time);
AudioQueueInputCallback cvl_capture_input_callback(void);
void cvl_capture_audio_queue_callback(void *context, AudioQueueRef queue,
                                      AudioQueueBufferRef input,
                                      const AudioTimeStamp *timestamp,
                                      UInt32 packet_count,
                                      const AudioStreamPacketDescription *packet_descriptions);
/* Writer-side: copies at most capacity_frames into caller-owned memory.
 * Read is allowed after a failure so recoverable accepted PCM can be drained.
 */
uint32_t cvl_capture_read_pcm(CVLCaptureBuffer *buffer, void *destination,
                              uint32_t capacity_frames);
bool cvl_capture_acknowledge_written(CVLCaptureBuffer *buffer, uint32_t frames);
/* Writer-only producer: publish computed extrema after acknowledging exact PCM.
 * The callback never computes extrema or publishes envelopes. Returns false for
 * stale/malformed/out-of-written-range metadata or a full telemetry ring. A full
 * ring increments telemetry_dropped_count; it never poisons the raw capture.
 */
bool cvl_capture_publish_envelope(CVLCaptureBuffer *buffer, const CVLCaptureEnvelope *envelope);
/* A blocked telemetry reader drops only envelopes, never raw PCM. Raw/written
 * counters remain independent of UI scheduling. Writer can build final waveform
 * directly from the exact PCM it writes, retaining peaks skipped by telemetry.
 */
bool cvl_capture_read_envelope(CVLCaptureBuffer *buffer, CVLCaptureEnvelope *envelope_out);
/* Atomic reads: intermediate snapshots are conservative observations, not final
 * receipts. Final receipt must follow queue disposal + complete writer drain.
 */
CVLCaptureSnapshot cvl_capture_snapshot(const CVLCaptureBuffer *buffer);
const char *cvl_capture_error_code(CVLCaptureError error);

#ifdef __cplusplus
}
#endif
#endif
