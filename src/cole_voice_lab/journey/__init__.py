"""voice-journey's longitudinal analysis, ported to Python (design §5.3).

The Voice Memos corpus (phone chain) and measurement sessions are indexed, filtered
for singing, transcribed locally, clustered into songs, measured and followed over
time. Every module here is the port of one voice-journey module; its tests are that
module's tests, translated case for case. Generated manifests keep voice-journey's
schema names and versions (`voice-journey.<name>.v1`) and are never committed.
"""
