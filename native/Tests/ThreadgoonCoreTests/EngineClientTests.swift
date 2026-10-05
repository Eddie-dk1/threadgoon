import Foundation
import Darwin
import Testing
@testable import Threadgoon

@Suite(.serialized)
struct EngineClientTests {
    @Test @MainActor func receivesSmallReadyPacketAndResponseWithoutWaitingForLargeBlock() async throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let workspace = root.appendingPathComponent(".build/swift-bridge-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: workspace) }
        let client = EngineClient()
        var receivedReady = false
        var receivedSnapshot = false
        var stopped = false
        var failure: String?
        client.onFailure = { failure = $0 }
        client.onMessage = { message in
            if message["type"] as? String == "ready" {
                receivedReady = true
                client.request("snapshot") { result in
                    if case .success(let data) = result { receivedSnapshot = data["workspace"] as? String == workspace.path }
                    client.shutdown { stopped = true }
                }
            }
        }
        try client.launch(workspace: workspace)
        let deadline = Date().addingTimeInterval(5)
        while !stopped && failure == nil && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        if client.running { client.shutdown { stopped = true } }
        #expect(failure == nil)
        #expect(receivedReady)
        #expect(receivedSnapshot)
        #expect(stopped)
        while client.running && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(!client.running)
    }

    @Test @MainActor func reportsWorkspaceLeaseFailureAndExits() async throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let workspace = root.appendingPathComponent(".build/swift-lease-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: workspace) }
        let first = EngineClient(), second = EngineClient()
        var ready = false
        var failure: String?
        first.onMessage = { if $0["type"] as? String == "ready" { ready = true } }
        second.onFailure = { failure = $0 }
        try first.launch(workspace: workspace)
        let deadline = Date().addingTimeInterval(5)
        while !ready && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(ready)
        try second.launch(workspace: workspace)
        while failure == nil && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(failure?.contains("другом Threadgoon") == true)
        first.shutdown {}
        while (first.running || second.running) && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(!first.running)
        #expect(!second.running)
    }

    @Test @MainActor func reportsCrashAndCanRestartSameWorkspace() async throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let workspace = root.appendingPathComponent(".build/swift-crash-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: workspace) }
        let client = EngineClient()
        var readyCount = 0
        var failure: String?
        client.onMessage = { if $0["type"] as? String == "ready" { readyCount += 1 } }
        client.onFailure = { failure = $0 }
        try client.launch(workspace: workspace)
        let deadline = Date().addingTimeInterval(7)
        while readyCount < 1 && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(readyCount == 1)
        let pid = try #require(client.processIdentifier)
        #expect(kill(pid, SIGKILL) == 0)
        while failure == nil && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(failure?.contains("перезапустите") == true)
        #expect(!client.running)
        try client.launch(workspace: workspace)
        while readyCount < 2 && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(readyCount == 2)
        client.shutdown {}
        while client.running && Date() < deadline { try await Task.sleep(for: .milliseconds(25)) }
        #expect(!client.running)
    }

}
