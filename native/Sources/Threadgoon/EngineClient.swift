import AppKit
import Foundation
import ThreadgoonCore

@MainActor
final class EngineClient {
    var onMessage: (([String: Any]) -> Void)?
    var onFailure: ((String) -> Void)?
    private var process: Process?
    private var input: FileHandle?
    private var requests: [String: (Result<[String: Any], Error>) -> Void] = [:]
    private let reader = DispatchQueue(label: "threadgoon.protocol.reader")
    private let writer = DispatchQueue(label: "threadgoon.protocol.writer")
    private var generation = UUID()
    private var stopping = false
    private var reportedFailure = false

    var running: Bool { process?.isRunning == true }
    var processIdentifier: Int32? { running ? process?.processIdentifier : nil }

    func launch(workspace: URL) throws {
        guard !running else { throw EngineError("Движок уже запущен") }
        generation = UUID()
        let currentGeneration = generation
        stopping = false
        reportedFailure = false
        let child = Process()
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        // The distributed .app always resolves its helper relative to its bundle.
        if let helper = Bundle.main.resourceURL?.appendingPathComponent("engine/threadgoon-engine"),
           FileManager.default.isExecutableFile(atPath: helper.path) {
            child.executableURL = helper
            child.arguments = ["--workspace", workspace.path]
        } else if let rootPath = ProcessInfo.processInfo.environment["THREADGOON_DEV_ROOT"] {
            let root = URL(fileURLWithPath: rootPath)
            child.executableURL = root.appendingPathComponent(".venv/bin/python")
            child.arguments = ["-m", "threadgoon.bridge", "--workspace", workspace.path]
            child.currentDirectoryURL = root
        } else {
            throw EngineError("В приложении отсутствует движок. Пересоберите .app.")
        }
        child.standardInput = stdin
        child.standardOutput = stdout
        child.standardError = stderr
        child.terminationHandler = { [weak self] child in
            Task { @MainActor in
                guard let self, self.generation == currentGeneration else { return }
                self.input = nil
                let callbacks = Array(self.requests.values)
                self.requests.removeAll()
                for callback in callbacks { callback(.failure(EngineError("Движок остановлен"))) }
                if !self.stopping && !self.reportedFailure {
                    self.onFailure?("Движок завершился (код \(child.terminationStatus)). Данные сохранены; перезапустите движок.")
                }
            }
        }
        try child.run()
        process = child
        input = stdin.fileHandleForWriting
        let output = stdout.fileHandleForReading
        reader.async { [weak self] in
            var framer = LineFramer()
            do {
                while true {
                    let bytes = output.availableData
                    if bytes.isEmpty { break }
                    for line in try framer.append(bytes) {
                        guard let message = try JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                            throw EngineError("Движок вернул некорректное сообщение")
                        }
                        Task { @MainActor in
                            guard let self, self.generation == currentGeneration else { return }
                            self.receive(message)
                        }
                    }
                }
            } catch {
                let description = error.localizedDescription
                Task { @MainActor in
                    guard let self, self.generation == currentGeneration, !self.stopping else { return }
                    self.onFailure?(description)
                }
            }
            try? output.close()
        }
        // Always drain stderr so diagnostics can never fill a pipe and freeze the helper.
        DispatchQueue.global(qos: .utility).async {
            let handle = stderr.fileHandleForReading
            while true {
                let data = handle.availableData
                if data.isEmpty { break }
                try? FileHandle.standardError.write(contentsOf: data)
            }
            try? handle.close()
        }
    }

    func request(_ command: String, _ params: [String: Any] = [:],
                 completion: @escaping (Result<[String: Any], Error>) -> Void = { _ in }) {
        guard let input, running, !stopping || command == "shutdown" else {
            completion(.failure(EngineError("Движок не запущен")))
            return
        }
        let id = UUID().uuidString
        do {
            let data = try Wire.request(id: id, command: command, params: params)
            requests[id] = completion
            writer.async { [weak self] in
                do { try input.write(contentsOf: data) }
                catch {
                    let description = error.localizedDescription
                    Task { @MainActor in self?.failRequest(id, description) }
                }
            }
        } catch { completion(.failure(error)) }
    }

    private func failRequest(_ id: String, _ description: String) {
        requests.removeValue(forKey: id)?(.failure(EngineError(description)))
    }

    private func receive(_ message: [String: Any]) {
        guard message["version"] as? Int == Wire.version else {
            onFailure?("Несовместимая версия движка. Пересоберите приложение.")
            return
        }
        if message["type"] as? String == "response", let id = message["id"] as? String {
            let callback = requests.removeValue(forKey: id)
            if message["ok"] as? Bool == true {
                callback?(.success(message["data"] as? [String: Any] ?? [:]))
            } else { callback?(.failure(EngineError(message["error"] as? String ?? "Ошибка команды"))) }
        } else if message["type"] as? String == "fatal" {
            reportedFailure = true
            onFailure?(message["error"] as? String ?? "Не удалось открыть рабочую папку")
        } else { onMessage?(message) }
    }

    func shutdown(completion: @escaping () -> Void) {
        guard running else { completion(); return }
        stopping = true
        request("shutdown") { [weak self] _ in
            try? self?.input?.close()
            completion()
        }
    }
}

struct EngineError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
