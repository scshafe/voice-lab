import Foundation

/// Python-compatible UTC timestamps; preserve microseconds in capture and wire validation.
public enum RecordingTimestamp {
    public static func string(_ date: Date) -> String {
        // ISO8601DateFormatter truncates sub-millisecond evidence. Python records
        // microseconds; preserve that precision at the native serialization boundary.
        let microseconds = (date.timeIntervalSince1970 * 1_000_000).rounded()
        let seconds = floor(microseconds / 1_000_000)
        let fraction = Int(microseconds - seconds * 1_000_000)
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter.string(from: Date(timeIntervalSince1970: seconds))
            + String(format: ".%06dZ", fraction)
    }

    public static func parse(_ text: String) -> Date? {
        if let date = try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(text) {
            return date
        }
        return try? Date.ISO8601FormatStyle(includingFractionalSeconds: false).parse(text)
    }
}

