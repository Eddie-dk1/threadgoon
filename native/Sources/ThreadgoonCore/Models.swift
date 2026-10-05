import Foundation

public struct Attachment: Decodable, Identifiable {
    public var id: String { "\(board)/\(tim)\(ext)" }
    public let board: String
    public let thread_id: Int
    public let tim: Int64
    public let ext: String
    public let display_name: String
    public let size: Int64?
    public let local_path: String?
}

public struct ThreadRecord: Decodable, Identifiable {
    public let id: String
    public let board: String
    public let thread_id: Int
    public let title: String
    public let source_url: String
    public let file_count: Int
    public let total_size: Int64?
    public let accuracy: String
    public let status: String
    public let snippet: String
    public let updated_at: Double
    public let attachments: [Attachment]
    public let thumbnail_url: String
    public let stale: Bool
}

public struct Board: Decodable, Identifiable {
    public var id: String { board }
    public let board: String
    public let title: String
    public let ws_board: Bool
    public let available: Bool
    public let error: String?
}

public struct DownloadJob: Decodable, Identifiable {
    public let id: Int
    public let board: String
    public let thread_id: Int
    public let destination: String
    public let status: String
    public let expected_size: Int64?
    public let downloaded_size: Int64
    public let error: String?
    public let updated_at: Double
    public var filename: String { URL(fileURLWithPath: destination).lastPathComponent }
    public var canCancel: Bool { ["waiting", "pending", "downloading", "paused"].contains(status) }
    public var progress: Double? {
        guard let size = expected_size, size > 0 else { return nil }
        return min(1, max(0, Double(downloaded_size) / Double(size)))
    }
}

public struct WatchRecord: Decodable, Identifiable {
    public let id: String
    public let board: String
    public let thread_id: Int
    public let interval: Int
    public let auto_download: Bool
    public let status: String
    public let last_checked: Double?
    public let last_error: String?
}

public struct AppConfig: Codable, Equatable {
    public var boards: [String] = []
    public var extensions: [String] = [".webm", ".mp4", ".gif", ".jpg", ".jpeg", ".png"]
    public var keywords: [String] = []
    public var keyword_scope = "file_post"
    public var min_file_size: Int64?
    public var max_file_size: Int64?
    public var min_matched_files = 1
    public var output_dir = "output"
    public var api_interval = 1.0
    public var watch_interval = 60
    public var media_workers = 3
    public var auto_download_new = false
    public init() {}
    public var json: [String: Any] {
        guard let data = try? JSONEncoder().encode(self),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        var complete = value
        if min_file_size == nil { complete["min_file_size"] = NSNull() }
        if max_file_size == nil { complete["max_file_size"] = NSNull() }
        return complete
    }
}

public struct EngineOperation: Decodable, Identifiable {
    public let id: String
    public let group: String
    public let state: String
    public let current: Int
    public let total: Int
    public let message: String
    public var paused: Bool { state == "paused" }
}

public struct Snapshot: Decodable {
    public let workspace: String
    public let config: AppConfig
    public let threads: [ThreadRecord]
    public let boards: [Board]
    public let downloads: [DownloadJob]
    public let watches: [WatchRecord]
    public let download_paused: Bool
    public let downloads_running: Bool
    public let operations: [EngineOperation]
}

public struct PreparedDownload: Decodable, Identifiable {
    public var id: String { token }
    public let token: String
    public let thread_count: Int
    public let file_count: Int
    public let total_size: Int64?
    public let warnings: [String]
}

public enum DownloadFilter: String, CaseIterable, Identifiable {
    case active = "Текущие", completed = "Завершённые", failed = "Ошибки", all = "Все"
    public var id: String { rawValue }
    public func includes(_ status: String) -> Bool {
        switch self {
        case .active: return ["pending", "downloading", "paused", "waiting"].contains(status)
        case .completed: return status == "completed"
        case .failed: return ["failed", "cancelled"].contains(status)
        case .all: return true
        }
    }
}

public func sizeText(_ value: Int64?) -> String {
    guard let value else { return "Размер неизвестен" }
    return ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
}

public func statusText(_ status: String) -> String {
    ["pending": "В очереди", "downloading": "Скачивается", "paused": "На паузе",
     "waiting": "Нужно подтверждение", "completed": "Готово", "failed": "Ошибка",
     "cancelled": "Отменено", "active": "Наблюдение включено",
     "available": "Доступен", "unavailable": "Недоступен", "expired": "Тред завершён",
     "error": "Ошибка"][status] ?? status
}

/// Decode incrementally: pipe reads may split UTF-8 scalars and JSON lines anywhere.
public struct LineFramer {
    private var buffer = Data()
    public init() {}
    public mutating func append(_ data: Data) throws -> [Data] {
        buffer.append(data)
        guard buffer.count <= 32 * 1024 * 1024 else {
            throw CocoaError(.fileReadTooLarge)
        }
        var lines: [Data] = []
        while let index = buffer.firstIndex(of: 10) {
            if index > buffer.startIndex { lines.append(Data(buffer[..<index])) }
            buffer.removeSubrange(...index)
        }
        return lines
    }
}

public enum Wire {
    public static let version = 1
    public static func request(id: String, command: String, params: [String: Any] = [:]) throws -> Data {
        var data = try JSONSerialization.data(withJSONObject: ["version": version, "id": id,
                                                               "command": command, "params": params])
        data.append(10)
        return data
    }
    public static func decode<T: Decodable>(_ type: T.Type, from object: Any) throws -> T {
        try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: object))
    }
}
