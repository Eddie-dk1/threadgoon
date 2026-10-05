import Foundation
import Testing
@testable import ThreadgoonCore

struct ProtocolTests {
    @Test func framingSplitUTF8AndMultipleMessages() throws {
        let bytes = Data("{\"text\":\"Привет\"}\n{\"id\":2}\n".utf8)
        var framer = LineFramer()
        var lines: [Data] = []
        for byte in bytes { lines += try framer.append(Data([byte])) }
        #expect(lines.count == 2)
        let object = try JSONSerialization.jsonObject(with: lines[0]) as? [String: String]
        #expect(object?["text"] == "Привет")
    }
    @Test func requestContainsVersionAndRoundTripsParams() throws {
        let bytes = try Wire.request(id: "request", command: "search", params: ["query": "русский\nтекст"])
        #expect(bytes.last == 10)
        let object = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
        #expect(object["version"] as? Int == 1)
        #expect(object["id"] as? String == "request")
        #expect((object["params"] as? [String: String])?["query"] == "русский\nтекст")
    }
    @Test func downloadFiltersAndStateDependentCancel() throws {
        let job = try Wire.decode(DownloadJob.self, from: ["id": 1, "board": "gif", "thread_id": 10,
            "destination": "output/example.jpg", "status": "completed", "expected_size": 3,
            "downloaded_size": 3, "updated_at": 0])
        #expect(!job.canCancel)
        #expect(job.progress == 1)
        #expect(DownloadFilter.completed.includes(job.status))
        #expect(!DownloadFilter.active.includes(job.status))
        #expect(DownloadFilter.failed.includes("cancelled"))
    }
    @Test func unknownSizeAndConfigRoundTrip() throws {
        #expect(sizeText(nil) == "Размер неизвестен")
        var config = AppConfig()
        config.boards = ["gif"]
        config.min_file_size = 1024
        let decoded = try Wire.decode(AppConfig.self, from: config.json)
        #expect(decoded == config)
        config.min_file_size = nil
        #expect(config.json["min_file_size"] is NSNull)
        #expect(config.json["max_file_size"] is NSNull)
    }
    @Test func versionedSnapshotDecodesNullableSizes() throws {
        var config = AppConfig(); config.boards = ["gif"]
        let snapshot = try Wire.decode(Snapshot.self, from: [
            "workspace": "/tmp/test", "config": config.json, "boards": [], "watches": [],
            "threads": [], "downloads": [], "operations": [], "download_paused": true,
            "downloads_running": false])
        #expect(snapshot.download_paused)
        #expect(snapshot.config.boards == ["gif"])
    }
}
