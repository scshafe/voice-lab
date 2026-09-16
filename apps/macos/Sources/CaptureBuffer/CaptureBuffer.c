#include "CaptureBuffer.h"

#include <math.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

struct CVLCaptureBuffer {
    CVLCaptureConfiguration configuration;
    unsigned char *pcm;
    CVLCaptureEnvelope *envelopes;
    _Atomic uint64_t raw_write_index;
    _Atomic uint64_t raw_read_index;
    _Atomic uint64_t written_frames;
    _Atomic uint64_t observed_frames;
    _Atomic uint64_t limit_discarded_frames;
    _Atomic uint64_t telemetry_write_index;
    _Atomic uint64_t telemetry_read_index;
    _Atomic uint64_t callback_count;
    _Atomic uint64_t late_callback_count;
    _Atomic uint64_t telemetry_dropped_count;
    _Atomic uint32_t active_callbacks;
    _Atomic int error;
    _Atomic int enqueue_os_status;
    _Atomic bool stop_requested;
    _Atomic bool maximum_reached;
    _Atomic bool queue_disposed;
    _Atomic bool has_sample_time;
    _Atomic double first_sample_time;
    _Atomic double next_sample_time;
    _Atomic uint64_t first_host_time;
    _Atomic bool first_host_time_valid;
    atomic_flag producer_active;
};

uint32_t cole_capture_buffer_abi_version(void) { return 1; }

CVLCaptureConfiguration cvl_capture_default_configuration(uint64_t generation) {
    return (CVLCaptureConfiguration){
        .sample_rate_hz = CVL_CAPTURE_SAMPLE_RATE,
        .channel_count = CVL_CAPTURE_CHANNELS,
        .bits_per_channel = CVL_CAPTURE_BITS_PER_CHANNEL,
        .bytes_per_frame = CVL_CAPTURE_FRAME_BYTES,
        .max_callback_frames = CVL_CAPTURE_DEFAULT_CALLBACK_FRAMES,
        .raw_capacity_frames = CVL_CAPTURE_DEFAULT_RAW_CAPACITY_FRAMES,
        .telemetry_capacity = CVL_CAPTURE_DEFAULT_TELEMETRY_CAPACITY,
        .maximum_frames = CVL_CAPTURE_MAXIMUM_FRAMES,
        .generation = generation,
    };
}

static bool configuration_valid(CVLCaptureConfiguration config) {
    return config.sample_rate_hz == CVL_CAPTURE_SAMPLE_RATE &&
           config.channel_count == CVL_CAPTURE_CHANNELS &&
           config.bits_per_channel == CVL_CAPTURE_BITS_PER_CHANNEL &&
           config.bytes_per_frame == CVL_CAPTURE_FRAME_BYTES &&
           config.max_callback_frames > 0 &&
           config.max_callback_frames <= CVL_CAPTURE_DEFAULT_CALLBACK_FRAMES &&
           config.raw_capacity_frames >= config.max_callback_frames &&
           config.raw_capacity_frames <= CVL_CAPTURE_MAX_RAW_CAPACITY_FRAMES &&
           config.telemetry_capacity > 0 &&
           config.telemetry_capacity <= CVL_CAPTURE_MAX_TELEMETRY_CAPACITY &&
           config.maximum_frames > 0 &&
           config.maximum_frames <= CVL_CAPTURE_MAXIMUM_FRAMES;
}

CVLCaptureBuffer *cvl_capture_create(CVLCaptureConfiguration configuration,
                                    CVLCaptureError *error_out) {
    if (error_out) { *error_out = CVL_CAPTURE_OK; }
    if (!configuration_valid(configuration)) {
        if (error_out) { *error_out = CVL_CAPTURE_INVALID_CONFIGURATION; }
        return NULL;
    }
    CVLCaptureBuffer *buffer = calloc(1, sizeof(*buffer));
    if (!buffer) {
        if (error_out) { *error_out = CVL_CAPTURE_ALLOCATION_FAILED; }
        return NULL;
    }
    buffer->configuration = configuration;
#define INITIALIZE_ATOMIC(member, value) atomic_init(&buffer->member, value)
    INITIALIZE_ATOMIC(raw_write_index, 0);
    INITIALIZE_ATOMIC(raw_read_index, 0);
    INITIALIZE_ATOMIC(written_frames, 0);
    INITIALIZE_ATOMIC(observed_frames, 0);
    INITIALIZE_ATOMIC(limit_discarded_frames, 0);
    INITIALIZE_ATOMIC(telemetry_write_index, 0);
    INITIALIZE_ATOMIC(telemetry_read_index, 0);
    INITIALIZE_ATOMIC(callback_count, 0);
    INITIALIZE_ATOMIC(late_callback_count, 0);
    INITIALIZE_ATOMIC(telemetry_dropped_count, 0);
    INITIALIZE_ATOMIC(active_callbacks, 0);
    INITIALIZE_ATOMIC(error, CVL_CAPTURE_OK);
    INITIALIZE_ATOMIC(enqueue_os_status, 0);
    INITIALIZE_ATOMIC(stop_requested, false);
    INITIALIZE_ATOMIC(maximum_reached, false);
    INITIALIZE_ATOMIC(queue_disposed, false);
    INITIALIZE_ATOMIC(has_sample_time, false);
    INITIALIZE_ATOMIC(first_sample_time, 0.0);
    INITIALIZE_ATOMIC(next_sample_time, 0.0);
    INITIALIZE_ATOMIC(first_host_time, 0);
    INITIALIZE_ATOMIC(first_host_time_valid, false);
#undef INITIALIZE_ATOMIC
    atomic_flag_clear(&buffer->producer_active);
    if (!atomic_is_lock_free(&buffer->raw_write_index) ||
        !atomic_is_lock_free(&buffer->active_callbacks) ||
        !atomic_is_lock_free(&buffer->stop_requested) ||
        !atomic_is_lock_free(&buffer->error) ||
        !atomic_is_lock_free(&buffer->next_sample_time)) {
        free(buffer);
        if (error_out) { *error_out = CVL_CAPTURE_NON_LOCK_FREE_ATOMICS; }
        return NULL;
    }
    buffer->pcm = calloc(configuration.raw_capacity_frames, CVL_CAPTURE_FRAME_BYTES);
    buffer->envelopes = calloc(configuration.telemetry_capacity, sizeof(*buffer->envelopes));
    if (!buffer->pcm || !buffer->envelopes) {
        free(buffer->pcm);
        free(buffer->envelopes);
        free(buffer);
        if (error_out) { *error_out = CVL_CAPTURE_ALLOCATION_FAILED; }
        return NULL;
    }
    /* Touch actual writable pages before the real-time callback owns memory. */
    volatile unsigned char *raw_pages = buffer->pcm;
    size_t raw_bytes = (size_t)configuration.raw_capacity_frames * CVL_CAPTURE_FRAME_BYTES;
    for (size_t offset = 0; offset < raw_bytes; offset += 4096) { raw_pages[offset] = 0; }
    raw_pages[raw_bytes - 1] = 0;
    volatile unsigned char *envelope_pages = (volatile unsigned char *)buffer->envelopes;
    size_t envelope_bytes = (size_t)configuration.telemetry_capacity * sizeof(*buffer->envelopes);
    for (size_t offset = 0; offset < envelope_bytes; offset += 4096) { envelope_pages[offset] = 0; }
    envelope_pages[envelope_bytes - 1] = 0;
    return buffer;
}

bool cvl_capture_format_is_canonical(const AudioStreamBasicDescription *format) {
    return format && format->mSampleRate == CVL_CAPTURE_SAMPLE_RATE &&
           format->mFormatID == kAudioFormatLinearPCM &&
           format->mFormatFlags == (kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked) &&
           format->mBytesPerPacket == CVL_CAPTURE_FRAME_BYTES && format->mFramesPerPacket == 1 &&
           format->mBytesPerFrame == CVL_CAPTURE_FRAME_BYTES &&
           format->mChannelsPerFrame == CVL_CAPTURE_CHANNELS &&
           format->mBitsPerChannel == CVL_CAPTURE_BITS_PER_CHANNEL && format->mReserved == 0;
}

void cvl_capture_request_stop(CVLCaptureBuffer *buffer) {
    if (buffer) { atomic_store_explicit(&buffer->stop_requested, true, memory_order_release); }
}

void cvl_capture_fail(CVLCaptureBuffer *buffer, CVLCaptureError error) {
    if (!buffer || error == CVL_CAPTURE_OK) { return; }
    int expected = CVL_CAPTURE_OK;
    atomic_compare_exchange_strong_explicit(&buffer->error, &expected, error,
                                            memory_order_release, memory_order_relaxed);
    cvl_capture_request_stop(buffer);
}

bool cvl_capture_mark_queue_disposed(CVLCaptureBuffer *buffer) {
    if (!buffer || !atomic_load_explicit(&buffer->stop_requested, memory_order_acquire) ||
        atomic_load_explicit(&buffer->active_callbacks, memory_order_acquire) != 0) {
        return false;
    }
    atomic_store_explicit(&buffer->queue_disposed, true, memory_order_release);
    return true;
}

bool cvl_capture_free(CVLCaptureBuffer *buffer) {
    if (!buffer) { return true; }
    if (!atomic_load_explicit(&buffer->queue_disposed, memory_order_acquire) ||
        atomic_load_explicit(&buffer->active_callbacks, memory_order_acquire) != 0) {
        return false;
    }
    free(buffer->pcm);
    free(buffer->envelopes);
    free(buffer);
    return true;
}

static bool enter_callback(CVLCaptureBuffer *buffer) {
    if (!buffer) { return false; }
    atomic_fetch_add_explicit(&buffer->active_callbacks, 1, memory_order_acq_rel);
    /* Stop flushes pending AudioQueue buffers: keep receiving them, but do not
     * enqueue again. Queue disposal, a fatal error or the explicit sample bound
     * closes input. Discarding at mere Stop request loses the final partial. */
    if (atomic_load_explicit(&buffer->queue_disposed, memory_order_acquire) ||
        atomic_load_explicit(&buffer->maximum_reached, memory_order_acquire) ||
        atomic_load_explicit(&buffer->error, memory_order_acquire) != CVL_CAPTURE_OK) {
        atomic_fetch_add_explicit(&buffer->late_callback_count, 1, memory_order_relaxed);
        atomic_fetch_sub_explicit(&buffer->active_callbacks, 1, memory_order_release);
        return false;
    }
    if (atomic_flag_test_and_set_explicit(&buffer->producer_active, memory_order_acquire)) {
        cvl_capture_fail(buffer, CVL_CAPTURE_CONCURRENT_PRODUCER);
        atomic_fetch_sub_explicit(&buffer->active_callbacks, 1, memory_order_release);
        return false;
    }
    return true;
}

static void leave_callback(CVLCaptureBuffer *buffer) {
    atomic_flag_clear_explicit(&buffer->producer_active, memory_order_release);
    atomic_fetch_sub_explicit(&buffer->active_callbacks, 1, memory_order_release);
}


static bool receive_pcm(CVLCaptureBuffer *buffer, const void *source, uint32_t byte_count,
                         uint32_t byte_capacity, double sample_time, uint32_t timestamp_flags,
                         uint64_t host_time) {
    if (byte_count == 0) { return true; }
    if (!source || byte_count > byte_capacity ||
        byte_count > buffer->configuration.max_callback_frames * CVL_CAPTURE_FRAME_BYTES) {
        cvl_capture_fail(buffer, CVL_CAPTURE_INVALID_BUFFER);
        return false;
    }
    if (byte_count % CVL_CAPTURE_FRAME_BYTES != 0) {
        cvl_capture_fail(buffer, CVL_CAPTURE_PARTIAL_PCM_FRAME);
        return false;
    }
    if (!(timestamp_flags & kAudioTimeStampSampleTimeValid) || !isfinite(sample_time)) {
        cvl_capture_fail(buffer, CVL_CAPTURE_INVALID_TIMESTAMP);
        return false;
    }
    bool has_sample_time = atomic_load_explicit(&buffer->has_sample_time, memory_order_relaxed);
    double expected = atomic_load_explicit(&buffer->next_sample_time, memory_order_relaxed);
    if (has_sample_time && fabs(sample_time - expected) > 0.000001) {
        cvl_capture_fail(buffer, CVL_CAPTURE_TIMESTAMP_DISCONTINUITY);
        return false;
    }
    uint32_t observed_frames = byte_count / CVL_CAPTURE_FRAME_BYTES;
    if (!isfinite(sample_time + observed_frames) || sample_time + observed_frames == sample_time) {
        cvl_capture_fail(buffer, CVL_CAPTURE_INVALID_TIMESTAMP);
        return false;
    }
    uint64_t write = atomic_load_explicit(&buffer->raw_write_index, memory_order_relaxed);
    uint64_t read = atomic_load_explicit(&buffer->raw_read_index, memory_order_acquire);
    uint64_t remaining = buffer->configuration.maximum_frames - write;
    uint32_t frames = observed_frames > remaining ? (uint32_t)remaining : observed_frames;
    if (write - read + frames > buffer->configuration.raw_capacity_frames) {
        cvl_capture_fail(buffer, CVL_CAPTURE_RAW_OVERFLOW);
        return false;
    }
    const unsigned char *pcm = source;
    uint32_t offset_frames = (uint32_t)(write % buffer->configuration.raw_capacity_frames);
    uint32_t first_frames = buffer->configuration.raw_capacity_frames - offset_frames;
    if (first_frames > frames) { first_frames = frames; }
    memcpy(buffer->pcm + (size_t)offset_frames * CVL_CAPTURE_FRAME_BYTES,
           pcm, (size_t)first_frames * CVL_CAPTURE_FRAME_BYTES);
    if (frames > first_frames) {
        memcpy(buffer->pcm, pcm + (size_t)first_frames * CVL_CAPTURE_FRAME_BYTES,
               (size_t)(frames - first_frames) * CVL_CAPTURE_FRAME_BYTES);
    }
    if (!has_sample_time) {
        atomic_store_explicit(&buffer->first_sample_time, sample_time, memory_order_relaxed);
        atomic_store_explicit(&buffer->first_host_time, host_time, memory_order_relaxed);
        atomic_store_explicit(&buffer->first_host_time_valid,
                              (timestamp_flags & kAudioTimeStampHostTimeValid) != 0,
                              memory_order_relaxed);
        atomic_store_explicit(&buffer->has_sample_time, true, memory_order_release);
    }
    atomic_store_explicit(&buffer->next_sample_time, sample_time + frames, memory_order_release);
    atomic_fetch_add_explicit(&buffer->observed_frames, observed_frames, memory_order_relaxed);
    atomic_fetch_add_explicit(&buffer->limit_discarded_frames, observed_frames - frames,
                              memory_order_relaxed);
    atomic_fetch_add_explicit(&buffer->callback_count, 1, memory_order_relaxed);
    atomic_store_explicit(&buffer->raw_write_index, write + frames, memory_order_release);
    if (write + frames == buffer->configuration.maximum_frames) {
        atomic_store_explicit(&buffer->maximum_reached, true, memory_order_release);
        cvl_capture_request_stop(buffer);
    }
    return true;
}

bool cvl_capture_receive_pcm(CVLCaptureBuffer *buffer, const void *pcm,
                             uint32_t byte_count, uint32_t byte_capacity,
                             double sample_time, uint32_t timestamp_flags,
                             uint64_t host_time) {
    if (!enter_callback(buffer)) { return false; }
    bool accepted = receive_pcm(buffer, pcm, byte_count, byte_capacity,
                                sample_time, timestamp_flags, host_time);
    leave_callback(buffer);
    return accepted;
}

AudioQueueInputCallback cvl_capture_input_callback(void) {
    return cvl_capture_audio_queue_callback;
}

void cvl_capture_audio_queue_callback(void *context, AudioQueueRef queue,
                                      AudioQueueBufferRef input,
                                      const AudioTimeStamp *timestamp,
                                      UInt32 packet_count,
                                      const AudioStreamPacketDescription *packet_descriptions) {
    (void)packet_count;
    (void)packet_descriptions;
    CVLCaptureBuffer *buffer = context;
    if (!enter_callback(buffer)) { return; }
    bool accepted = false;
    if (!queue || !input || !timestamp) {
        cvl_capture_fail(buffer, CVL_CAPTURE_INVALID_BUFFER);
    } else {
        accepted = receive_pcm(buffer, input->mAudioData, input->mAudioDataByteSize,
                               input->mAudioDataBytesCapacity, timestamp->mSampleTime,
                               timestamp->mFlags, timestamp->mHostTime);
    }
    if (accepted && !atomic_load_explicit(&buffer->stop_requested, memory_order_acquire)) {
        OSStatus status = AudioQueueEnqueueBuffer(queue, input, 0, NULL);
        if (status != noErr) {
            atomic_store_explicit(&buffer->enqueue_os_status, status, memory_order_release);
            cvl_capture_fail(buffer, CVL_CAPTURE_ENQUEUE_FAILED);
        }
    }
    leave_callback(buffer);
}

uint32_t cvl_capture_read_pcm(CVLCaptureBuffer *buffer, void *destination,
                              uint32_t capacity_frames) {
    if (!buffer || !destination || capacity_frames == 0) { return 0; }
    uint64_t read = atomic_load_explicit(&buffer->raw_read_index, memory_order_relaxed);
    uint64_t write = atomic_load_explicit(&buffer->raw_write_index, memory_order_acquire);
    uint64_t available = write - read;
    uint32_t frames = available < capacity_frames ? (uint32_t)available : capacity_frames;
    uint32_t offset_frames = (uint32_t)(read % buffer->configuration.raw_capacity_frames);
    uint32_t first_frames = buffer->configuration.raw_capacity_frames - offset_frames;
    if (first_frames > frames) { first_frames = frames; }
    memcpy(destination, buffer->pcm + (size_t)offset_frames * CVL_CAPTURE_FRAME_BYTES,
           (size_t)first_frames * CVL_CAPTURE_FRAME_BYTES);
    if (frames > first_frames) {
        memcpy((unsigned char *)destination + (size_t)first_frames * CVL_CAPTURE_FRAME_BYTES,
               buffer->pcm, (size_t)(frames - first_frames) * CVL_CAPTURE_FRAME_BYTES);
    }
    atomic_store_explicit(&buffer->raw_read_index, read + frames, memory_order_release);
    return frames;
}

bool cvl_capture_acknowledge_written(CVLCaptureBuffer *buffer, uint32_t frames) {
    if (!buffer) { return false; }
    uint64_t written = atomic_load_explicit(&buffer->written_frames, memory_order_relaxed);
    uint64_t dequeued = atomic_load_explicit(&buffer->raw_read_index, memory_order_acquire);
    if (frames == 0 || frames > dequeued - written) {
        cvl_capture_fail(buffer, CVL_CAPTURE_INVALID_WRITE_ACKNOWLEDGMENT);
        return false;
    }
    atomic_store_explicit(&buffer->written_frames, written + frames, memory_order_release);
    return true;
}

bool cvl_capture_publish_envelope(CVLCaptureBuffer *buffer, const CVLCaptureEnvelope *envelope) {
    if (!buffer || !envelope || envelope->generation != buffer->configuration.generation ||
        envelope->frame_count == 0 || envelope->minimum_pcm < -8388608 ||
        envelope->maximum_pcm > 8388607 || envelope->minimum_pcm > envelope->maximum_pcm ||
        !isfinite(envelope->sample_time)) {
        return false;
    }
    uint64_t written = atomic_load_explicit(&buffer->written_frames, memory_order_acquire);
    if (envelope->start_frame > written || envelope->frame_count > written - envelope->start_frame) {
        return false;
    }
    uint64_t write = atomic_load_explicit(&buffer->telemetry_write_index, memory_order_relaxed);
    uint64_t read = atomic_load_explicit(&buffer->telemetry_read_index, memory_order_acquire);
    if (write - read == buffer->configuration.telemetry_capacity) {
        atomic_fetch_add_explicit(&buffer->telemetry_dropped_count, 1, memory_order_relaxed);
        return false;
    }
    buffer->envelopes[write % buffer->configuration.telemetry_capacity] = *envelope;
    atomic_store_explicit(&buffer->telemetry_write_index, write + 1, memory_order_release);
    return true;
}

bool cvl_capture_read_envelope(CVLCaptureBuffer *buffer, CVLCaptureEnvelope *envelope_out) {
    if (!buffer || !envelope_out) { return false; }
    uint64_t read = atomic_load_explicit(&buffer->telemetry_read_index, memory_order_relaxed);
    uint64_t write = atomic_load_explicit(&buffer->telemetry_write_index, memory_order_acquire);
    if (read == write) { return false; }
    *envelope_out = buffer->envelopes[read % buffer->configuration.telemetry_capacity];
    atomic_store_explicit(&buffer->telemetry_read_index, read + 1, memory_order_release);
    return true;
}

CVLCaptureSnapshot cvl_capture_snapshot(const CVLCaptureBuffer *buffer) {
    CVLCaptureSnapshot snapshot = {0};
    if (!buffer) { snapshot.error = CVL_CAPTURE_INVALID_CONFIGURATION; return snapshot; }
    snapshot.generation = buffer->configuration.generation;
    snapshot.written_frames = atomic_load_explicit(&buffer->written_frames, memory_order_acquire);
    snapshot.dequeued_frames = atomic_load_explicit(&buffer->raw_read_index, memory_order_acquire);
    snapshot.received_frames = atomic_load_explicit(&buffer->raw_write_index, memory_order_acquire);
    snapshot.queued_frames = (uint32_t)(snapshot.received_frames - snapshot.dequeued_frames);
#define SNAPSHOT(member) snapshot.member = atomic_load_explicit(&buffer->member, memory_order_acquire)
    SNAPSHOT(observed_frames);
    SNAPSHOT(limit_discarded_frames);
    SNAPSHOT(callback_count);
    SNAPSHOT(late_callback_count);
    SNAPSHOT(telemetry_dropped_count);
    SNAPSHOT(active_callbacks);
    SNAPSHOT(first_sample_time);
    SNAPSHOT(next_sample_time);
    SNAPSHOT(first_host_time);
    SNAPSHOT(first_host_time_valid);
    SNAPSHOT(has_sample_time);
    SNAPSHOT(stop_requested);
    SNAPSHOT(maximum_reached);
    SNAPSHOT(queue_disposed);
    SNAPSHOT(enqueue_os_status);
#undef SNAPSHOT
    snapshot.error = (CVLCaptureError)atomic_load_explicit(&buffer->error, memory_order_acquire);
    return snapshot;
}

const char *cvl_capture_error_code(CVLCaptureError error) {
    switch (error) {
#define ERROR_CASE(code) case CVL_CAPTURE_##code: return "CAPTURE." #code
        ERROR_CASE(OK);
        ERROR_CASE(INVALID_CONFIGURATION);
        ERROR_CASE(ALLOCATION_FAILED);
        ERROR_CASE(NON_LOCK_FREE_ATOMICS);
        ERROR_CASE(INVALID_BUFFER);
        ERROR_CASE(PARTIAL_PCM_FRAME);
        ERROR_CASE(INVALID_TIMESTAMP);
        ERROR_CASE(TIMESTAMP_DISCONTINUITY);
        ERROR_CASE(RAW_OVERFLOW);
        ERROR_CASE(CONCURRENT_PRODUCER);
        ERROR_CASE(ENQUEUE_FAILED);
        ERROR_CASE(INVALID_WRITE_ACKNOWLEDGMENT);
        ERROR_CASE(DEVICE_CHANGED);
        ERROR_CASE(FORMAT_CHANGED);
        ERROR_CASE(WRITER_FAILED);
        ERROR_CASE(PARENT_LOST);
#undef ERROR_CASE
    }
    return "CAPTURE.UNKNOWN_ERROR_CODE";
}
