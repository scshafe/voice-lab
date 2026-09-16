#include "CaptureBuffer.h"

#include <assert.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

/* Test symbol replaces the one permitted framework call. No AudioQueue is
 * created or started by this executable. Input callback tests use inert memory. */
static unsigned enqueue_calls;
static OSStatus enqueue_result;
static CVLCaptureBuffer *reentrant_buffer;
OSStatus AudioQueueEnqueueBuffer(AudioQueueRef queue, AudioQueueBufferRef buffer,
                                UInt32 count, const AudioStreamPacketDescription *descriptions) {
    assert(queue != NULL && buffer != NULL && count == 0 && descriptions == NULL);
    enqueue_calls++;
    if (reentrant_buffer) {
        unsigned char zero[3] = {0};
        assert(!cvl_capture_receive_pcm(reentrant_buffer, zero, 3, 3, 0, 1, 0));
    }
    return enqueue_result;
}

static CVLCaptureBuffer *make_buffer(uint32_t capacity, uint32_t max_callback,
                                     uint32_t telemetry_capacity, uint64_t maximum_frames) {
    CVLCaptureConfiguration config = cvl_capture_default_configuration(7);
    config.raw_capacity_frames = capacity;
    config.max_callback_frames = max_callback;
    config.telemetry_capacity = telemetry_capacity;
    config.maximum_frames = maximum_frames;
    CVLCaptureError error = CVL_CAPTURE_INVALID_CONFIGURATION;
    CVLCaptureBuffer *buffer = cvl_capture_create(config, &error);
    assert(buffer && error == CVL_CAPTURE_OK);
    return buffer;
}

static void finish(CVLCaptureBuffer *buffer) {
    cvl_capture_request_stop(buffer);
    assert(cvl_capture_mark_queue_disposed(buffer));
    assert(cvl_capture_free(buffer));
}

static void fill(unsigned char *bytes, uint32_t first, uint32_t frames) {
    for (uint32_t frame = 0; frame < frames; ++frame) {
        uint32_t value = first + frame;
        bytes[frame * 3] = (unsigned char)value;
        bytes[frame * 3 + 1] = (unsigned char)(value >> 8);
        bytes[frame * 3 + 2] = (unsigned char)(value >> 16);
    }
}

static bool receive(CVLCaptureBuffer *buffer, const void *pcm, uint32_t frames, double time) {
    return cvl_capture_receive_pcm(buffer, pcm, frames * 3, frames * 3, time,
                                   kAudioTimeStampSampleTimeValid | kAudioTimeStampHostTimeValid, 1234);
}

static void test_exact_variable_buffers_and_ring_wrap(void) {
    CVLCaptureBuffer *buffer = make_buffer(13, 8, 4, 1000);
    unsigned char expected[39], actual[39];
    fill(expected, 0, 13);
    assert(receive(buffer, expected, 8, -123.0));
    assert(cvl_capture_snapshot(buffer).received_frames == 8);
    assert(cvl_capture_snapshot(buffer).written_frames == 0);
    assert(cvl_capture_read_pcm(buffer, actual, 5) == 5);
    assert(cvl_capture_snapshot(buffer).written_frames == 0);
    assert(cvl_capture_acknowledge_written(buffer, 5));
    assert(receive(buffer, expected + 24, 5, -115.0));
    assert(cvl_capture_read_pcm(buffer, actual + 15, 8) == 8);
    assert(memcmp(actual, expected, sizeof(actual)) == 0);
    assert(cvl_capture_acknowledge_written(buffer, 8));
    /* Index 13 wraps to zero and the next input is still byte exact. */
    assert(receive(buffer, expected, 7, -110.0));
    assert(cvl_capture_read_pcm(buffer, actual, 7) == 7);
    assert(memcmp(actual, expected, 21) == 0);
    assert(cvl_capture_acknowledge_written(buffer, 7));
    CVLCaptureSnapshot snapshot = cvl_capture_snapshot(buffer);
    assert(snapshot.received_frames == 20 && snapshot.written_frames == 20);
    assert(snapshot.observed_frames == 20 && snapshot.callback_count == 3);
    assert(snapshot.first_sample_time == -123.0 && snapshot.next_sample_time == -103.0);
    assert(snapshot.generation == 7 && snapshot.queued_frames == 0);
    finish(buffer);
}

static void test_envelope_signed_extrema_silence_and_blocked_telemetry(void) {
    CVLCaptureBuffer *buffer = make_buffer(16, 8, 1, 1000);
    const unsigned char pcm[] = {0x00, 0x00, 0x80, 0xff, 0xff, 0x7f, 0, 0, 0};
    unsigned char output[9];
    for (unsigned batch = 0; batch < 100; ++batch) {
        assert(receive(buffer, pcm, 3, batch * 3));
        assert(cvl_capture_read_pcm(buffer, output, 3) == 3);
        assert(memcmp(output, pcm, sizeof(pcm)) == 0);
        assert(cvl_capture_acknowledge_written(buffer, 3));
        CVLCaptureEnvelope envelope = {
            .generation = 7, .start_frame = batch * 3, .frame_count = 3,
            .minimum_pcm = -8388608, .maximum_pcm = 8388607,
            .sample_time = batch * 3, .host_time = 1234, .host_time_valid = true,
        };
        assert(cvl_capture_publish_envelope(buffer, &envelope) == (batch == 0));
    }
    CVLCaptureSnapshot snapshot = cvl_capture_snapshot(buffer);
    assert(snapshot.error == CVL_CAPTURE_OK && snapshot.written_frames == 300);
    assert(snapshot.telemetry_dropped_count == 99);
    CVLCaptureEnvelope envelope;
    assert(cvl_capture_read_envelope(buffer, &envelope));
    assert(envelope.minimum_pcm == -8388608 && envelope.maximum_pcm == 8388607);
    assert(envelope.start_frame == 0 && envelope.frame_count == 3);
    assert(envelope.host_time_valid && envelope.host_time == 1234 && envelope.generation == 7);
    assert(!cvl_capture_read_envelope(buffer, &envelope));
    unsigned char zeros[9] = {0};
    assert(receive(buffer, zeros, 3, 300));
    assert(cvl_capture_read_pcm(buffer, output, 3) == 3);
    assert(cvl_capture_acknowledge_written(buffer, 3));
    envelope = (CVLCaptureEnvelope){.generation = 7, .start_frame = 300,
                                   .frame_count = 3, .sample_time = 300};
    assert(cvl_capture_publish_envelope(buffer, &envelope));
    assert(cvl_capture_read_envelope(buffer, &envelope));
    assert(envelope.minimum_pcm == 0 && envelope.maximum_pcm == 0);
    envelope.generation = 8;
    assert(!cvl_capture_publish_envelope(buffer, &envelope));
    envelope.generation = 7;
    envelope.start_frame = 302;
    assert(!cvl_capture_publish_envelope(buffer, &envelope));
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_OK);
    finish(buffer);
}

static void test_timestamp_gap_overlap_invalid_and_tolerance(void) {
    const double invalid[] = {NAN, INFINITY, -INFINITY, 1e300};
    unsigned char pcm[6] = {0};
    for (unsigned index = 0; index < sizeof(invalid) / sizeof(invalid[0]); ++index) {
        CVLCaptureBuffer *buffer = make_buffer(8, 4, 1, 100);
        assert(!receive(buffer, pcm, 2, invalid[index]));
        assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_TIMESTAMP);
        finish(buffer);
    }
    const double gap[] = {1.0, -1.0, 0.0000011, -0.0000011};
    for (unsigned index = 0; index < sizeof(gap) / sizeof(gap[0]); ++index) {
        CVLCaptureBuffer *buffer = make_buffer(8, 4, 1, 100);
        assert(receive(buffer, pcm, 2, 10.0));
        assert(!receive(buffer, pcm, 2, 12.0 + gap[index]));
        CVLCaptureSnapshot snapshot = cvl_capture_snapshot(buffer);
        assert(snapshot.error == CVL_CAPTURE_TIMESTAMP_DISCONTINUITY);
        assert(snapshot.received_frames == 2 && snapshot.stop_requested);
        finish(buffer);
    }
    CVLCaptureBuffer *buffer = make_buffer(8, 4, 1, 100);
    assert(receive(buffer, pcm, 2, 10));
    assert(receive(buffer, pcm, 2, 12.0000005));
    assert(!cvl_capture_receive_pcm(buffer, pcm, 6, 6, 14.0000005, 0, 0));
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_TIMESTAMP);
    finish(buffer);
}

static void test_overflow_preserves_existing_pcm_and_first_error(void) {
    CVLCaptureBuffer *buffer = make_buffer(4, 4, 1, 100);
    unsigned char pcm[12], output[12];
    fill(pcm, 3, 4);
    assert(receive(buffer, pcm, 4, 0));
    assert(!receive(buffer, pcm, 1, 4));
    cvl_capture_fail(buffer, CVL_CAPTURE_WRITER_FAILED);
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_RAW_OVERFLOW);
    assert(cvl_capture_snapshot(buffer).received_frames == 4);
    assert(cvl_capture_read_pcm(buffer, output, 4) == 4);
    assert(memcmp(pcm, output, sizeof(pcm)) == 0);
    assert(cvl_capture_acknowledge_written(buffer, 4));
    finish(buffer);
}

static void test_format_and_configuration_reject_changes(void) {
    AudioStreamBasicDescription format = {
        .mSampleRate = 48000, .mFormatID = kAudioFormatLinearPCM,
        .mFormatFlags = kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
        .mBytesPerPacket = 3, .mFramesPerPacket = 1, .mBytesPerFrame = 3,
        .mChannelsPerFrame = 1, .mBitsPerChannel = 24,
    };
    assert(cvl_capture_format_is_canonical(&format));
    format.mSampleRate = 44100;
    assert(!cvl_capture_format_is_canonical(&format));
    format.mSampleRate = 48000;
    format.mFormatFlags |= kAudioFormatFlagIsBigEndian;
    assert(!cvl_capture_format_is_canonical(&format));
    assert(!cvl_capture_format_is_canonical(NULL));
    CVLCaptureConfiguration config = cvl_capture_default_configuration(0);
    CVLCaptureError error;
    config.raw_capacity_frames = 1;
    assert(cvl_capture_create(config, &error) == NULL);
    assert(error == CVL_CAPTURE_INVALID_CONFIGURATION);
    config = cvl_capture_default_configuration(0);
    config.maximum_frames = CVL_CAPTURE_MAXIMUM_FRAMES + 1;
    assert(cvl_capture_create(config, &error) == NULL);
    config = cvl_capture_default_configuration(0);
    config.bytes_per_frame = 4;
    assert(cvl_capture_create(config, &error) == NULL);
    assert(cole_capture_buffer_abi_version() == 1);
}

static void test_invalid_partial_null_and_oversized_input(void) {
    unsigned char pcm[12] = {0};
    CVLCaptureBuffer *buffer = make_buffer(4, 4, 1, 100);
    assert(!cvl_capture_receive_pcm(buffer, pcm, 4, 12, 0, 1, 0));
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_PARTIAL_PCM_FRAME);
    finish(buffer);
    buffer = make_buffer(4, 4, 1, 100);
    assert(!cvl_capture_receive_pcm(buffer, NULL, 3, 12, 0, 1, 0));
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_BUFFER);
    finish(buffer);
    buffer = make_buffer(4, 4, 1, 100);
    assert(!cvl_capture_receive_pcm(buffer, pcm, 9, 6, 0, 1, 0));
    assert(cvl_capture_snapshot(buffer).received_frames == 0);
    finish(buffer);
    buffer = make_buffer(4, 4, 1, 100);
    assert(!cvl_capture_receive_pcm(buffer, pcm, 15, 15, 0, 1, 0));
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_BUFFER);
    finish(buffer);
}

static void test_written_acknowledgment_is_independent_and_validated(void) {
    CVLCaptureBuffer *buffer = make_buffer(4, 4, 1, 100);
    unsigned char pcm[12] = {0}, output[12];
    assert(receive(buffer, pcm, 4, 0));
    assert(cvl_capture_read_pcm(buffer, output, 4) == 4);
    assert(cvl_capture_acknowledge_written(buffer, 2));
    assert(cvl_capture_snapshot(buffer).written_frames == 2);
    assert(!cvl_capture_acknowledge_written(buffer, 3));
    assert(cvl_capture_snapshot(buffer).written_frames == 2);
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_WRITE_ACKNOWLEDGMENT);
    finish(buffer);
}

static void test_exact_maximum_preserves_and_reports_partial_limit_buffer(void) {
    CVLCaptureBuffer *buffer = make_buffer(16, 8, 4, 5);
    unsigned char pcm[18], output[18];
    fill(pcm, 0, 6);
    assert(receive(buffer, pcm, 3, 0));
    assert(receive(buffer, pcm + 9, 3, 3));
    CVLCaptureSnapshot snapshot = cvl_capture_snapshot(buffer);
    assert(snapshot.maximum_reached && snapshot.stop_requested && snapshot.error == CVL_CAPTURE_OK);
    assert(snapshot.received_frames == 5 && snapshot.observed_frames == 6);
    assert(snapshot.limit_discarded_frames == 1 && snapshot.next_sample_time == 5);
    assert(!receive(buffer, pcm, 1, 6));
    assert(cvl_capture_snapshot(buffer).late_callback_count == 1);
    assert(cvl_capture_read_pcm(buffer, output, 6) == 5);
    assert(memcmp(output, pcm, 15) == 0);
    assert(cvl_capture_acknowledge_written(buffer, 5));
    finish(buffer);
}

static void test_callback_stop_drains_without_reenqueue_and_late_callback_is_ignored(void) {
    CVLCaptureBuffer *buffer = make_buffer(16, 8, 4, 100);
    unsigned char pcm[6] = {1, 0, 0, 2, 0, 0};
    AudioQueueBuffer input = {.mAudioDataBytesCapacity = 6, .mAudioDataByteSize = 6, .mAudioData = pcm};
    AudioTimeStamp timestamp = {.mSampleTime = 0, .mFlags = kAudioTimeStampSampleTimeValid};
    AudioQueueRef queue = (AudioQueueRef)(uintptr_t)1;
    enqueue_calls = 0;
    enqueue_result = noErr;
    cvl_capture_audio_queue_callback(buffer, queue, &input, &timestamp, 0, NULL);
    assert(enqueue_calls == 1 && cvl_capture_snapshot(buffer).received_frames == 2);
    assert(!cvl_capture_free(buffer));
    assert(!cvl_capture_mark_queue_disposed(buffer));
    cvl_capture_request_stop(buffer);
    timestamp.mSampleTime = 2;
    cvl_capture_audio_queue_callback(buffer, queue, &input, &timestamp, 0, NULL);
    assert(enqueue_calls == 1 && cvl_capture_snapshot(buffer).received_frames == 4);
    assert(cvl_capture_mark_queue_disposed(buffer));
    cvl_capture_audio_queue_callback(buffer, queue, NULL, NULL, 0, NULL);
    assert(cvl_capture_snapshot(buffer).late_callback_count == 1);
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_OK);
    assert(cvl_capture_free(buffer));
}

static void test_callback_empty_start_cancel_invalid_and_enqueue_failure(void) {
    CVLCaptureBuffer *buffer = make_buffer(16, 8, 4, 100);
    AudioQueueBuffer input = {0};
    AudioTimeStamp timestamp = {0};
    AudioQueueRef queue = (AudioQueueRef)(uintptr_t)1;
    enqueue_calls = 0;
    enqueue_result = noErr;
    cvl_capture_audio_queue_callback(buffer, queue, &input, &timestamp, 0, NULL);
    assert(cvl_capture_snapshot(buffer).callback_count == 0 && enqueue_calls == 1);
    cvl_capture_request_stop(buffer);
    cvl_capture_audio_queue_callback(buffer, queue, &input, &timestamp, 0, NULL);
    assert(cvl_capture_snapshot(buffer).received_frames == 0 && enqueue_calls == 1);
    finish(buffer);
    buffer = make_buffer(16, 8, 4, 100);
    cvl_capture_audio_queue_callback(buffer, NULL, &input, &timestamp, 0, NULL);
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_INVALID_BUFFER);
    finish(buffer);
    buffer = make_buffer(16, 8, 4, 100);
    enqueue_result = -12345;
    cvl_capture_audio_queue_callback(buffer, queue, &input, &timestamp, 0, NULL);
    assert(cvl_capture_snapshot(buffer).error == CVL_CAPTURE_ENQUEUE_FAILED);
    assert(cvl_capture_snapshot(buffer).enqueue_os_status == -12345);
    finish(buffer);
}

static void test_concurrent_reentrant_producer_fails_without_waiting(void) {
    CVLCaptureBuffer *buffer = make_buffer(16, 8, 4, 100);
    unsigned char zero[3] = {0};
    AudioQueueBuffer input = {.mAudioDataBytesCapacity = 3, .mAudioDataByteSize = 3,
                              .mAudioData = zero};
    AudioTimeStamp timestamp = {.mSampleTime = 0, .mFlags = kAudioTimeStampSampleTimeValid};
    enqueue_result = noErr;
    reentrant_buffer = buffer;
    cvl_capture_audio_queue_callback(buffer, (AudioQueueRef)(uintptr_t)1,
                                     &input, &timestamp, 0, NULL);
    reentrant_buffer = NULL;
    CVLCaptureSnapshot snapshot = cvl_capture_snapshot(buffer);
    assert(snapshot.error == CVL_CAPTURE_CONCURRENT_PRODUCER && snapshot.stop_requested);
    assert(snapshot.active_callbacks == 0 && snapshot.received_frames == 1);
    finish(buffer);
}

typedef struct {
    CVLCaptureBuffer *buffer;
    _Atomic bool producer_finished;
    _Atomic uint64_t checked_frames;
} ConcurrentTest;

static void *produce(void *argument) {
    ConcurrentTest *test = argument;
    unsigned char pcm[51];
    for (uint32_t frame = 0; frame < 400000;) {
        uint32_t count = 1 + frame % 17;
        if (frame + count > 400000) { count = 400000 - frame; }
        /* Test producer synchronization is outside the bounded real callback. */
        while (cvl_capture_snapshot(test->buffer).queued_frames > 64 - count) { sched_yield(); }
        fill(pcm, frame, count);
        assert(receive(test->buffer, pcm, count, frame));
        frame += count;
    }
    atomic_store_explicit(&test->producer_finished, true, memory_order_release);
    return NULL;
}

static void *consume(void *argument) {
    ConcurrentTest *test = argument;
    unsigned char actual[69], expected[69];
    uint32_t frame = 0;
    while (!atomic_load_explicit(&test->producer_finished, memory_order_acquire) ||
           cvl_capture_snapshot(test->buffer).queued_frames > 0) {
        uint32_t read = cvl_capture_read_pcm(test->buffer, actual, 23);
        if (read == 0) { sched_yield(); continue; }
        fill(expected, frame, read);
        assert(memcmp(actual, expected, read * 3) == 0);
        frame += read;
        assert(cvl_capture_acknowledge_written(test->buffer, read));
    }
    atomic_store_explicit(&test->checked_frames, frame, memory_order_release);
    return NULL;
}

static void test_concurrent_spsc_producer_consumer_exactness(void) {
    ConcurrentTest test = {.buffer = make_buffer(64, 17, 1, 500000)};
    atomic_init(&test.producer_finished, false);
    atomic_init(&test.checked_frames, 0);
    pthread_t producer, consumer;
    assert(pthread_create(&producer, NULL, produce, &test) == 0);
    assert(pthread_create(&consumer, NULL, consume, &test) == 0);
    assert(pthread_join(producer, NULL) == 0);
    assert(pthread_join(consumer, NULL) == 0);
    assert(atomic_load(&test.checked_frames) == 400000);
    CVLCaptureSnapshot snapshot = cvl_capture_snapshot(test.buffer);
    assert(snapshot.received_frames == 400000 && snapshot.written_frames == 400000);
    assert(snapshot.error == CVL_CAPTURE_OK && snapshot.active_callbacks == 0);
    finish(test.buffer);
}

int main(void) {
    test_exact_variable_buffers_and_ring_wrap();
    test_envelope_signed_extrema_silence_and_blocked_telemetry();
    test_timestamp_gap_overlap_invalid_and_tolerance();
    test_overflow_preserves_existing_pcm_and_first_error();
    test_format_and_configuration_reject_changes();
    test_invalid_partial_null_and_oversized_input();
    test_written_acknowledgment_is_independent_and_validated();
    test_exact_maximum_preserves_and_reports_partial_limit_buffer();
    test_callback_stop_drains_without_reenqueue_and_late_callback_is_ignored();
    test_callback_empty_start_cancel_invalid_and_enqueue_failure();
    test_concurrent_reentrant_producer_fails_without_waiting();
    test_concurrent_spsc_producer_consumer_exactness();
    puts("{\"suite\":\"capture-buffer\",\"tests\":12,\"synthetic_only\":true,\"status\":\"passed\"}");
    return 0;
}
