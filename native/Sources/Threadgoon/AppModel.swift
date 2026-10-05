import AppKit
import SwiftUI
import ThreadgoonCore

@MainActor
final class AppModel: ObservableObject {
    enum Section: String, CaseIterable, Identifiable {
        case catalog = "Каталог", downloads = "Загрузки", watches = "Наблюдение"
        var id: String { rawValue }
        var symbol: String {
            switch self { case .catalog: return "square.stack"; case .downloads: return "arrow.down.circle"; case .watches: return "eye" }
        }
    }
    @Published var section: Section = .catalog
    @Published var workspace: URL?
    @Published var ready = false
    @Published var opening = false
    @Published var engineFailed = false
    @Published var error: String?
    @Published var status = "Выберите рабочую папку"
    @Published var config = AppConfig()
    @Published var filters = AppConfig()
    @Published var threads: [ThreadRecord] = []
    @Published var boards: [Board] = []
    @Published var downloads: [DownloadJob] = []
    @Published var watches: [WatchRecord] = []
    @Published var operations: [EngineOperation] = []
    @Published var selectedThreads = Set<String>()
    @Published var selectedDownloads = Set<Int>()
    @Published var detail: ThreadRecord?
    @Published var query = ""
    @Published var exact = false
    @Published var filtersVisible = false
    @Published var downloadPaused = false
    @Published var downloadsRunning = false
    @Published var downloadFilter: DownloadFilter = .active
    @Published var prepared: PreparedDownload?
    @Published var showingBoards = false
    @Published var showingDiagnostics = false
    @Published var diagnosticText = ""
    @Published var showingHelp = false
    @Published var appearance = "system"
    let engine = EngineClient()
    private var hasSearchResults = false
    private var initialConfigLoaded = false
    private var detailRequestedID: String?
    private var pendingDetailKey: String?
    private var submitting = Set<String>()

    var searchOperation: EngineOperation? { operations.first { $0.group == "search" } }
    var busy: Bool { !operations.isEmpty || downloadsRunning || !submitting.isEmpty }
    var preparing: Bool { operations.contains { ["prepare", "watch-add"].contains($0.group) } || submitting.contains("prepare_download") || submitting.contains("watch_add") }
    var searching: Bool { searchOperation != nil || submitting.contains("search") }
    var visibleDownloads: [DownloadJob] { downloads.filter { downloadFilter.includes($0.status) } }
    var selectedJobs: [DownloadJob] { visibleDownloads.filter { selectedDownloads.contains($0.id) } }
    var colorScheme: ColorScheme? { appearance == "light" ? .light : appearance == "dark" ? .dark : nil }

    init() {
        engine.onMessage = { [weak self] message in self?.receive(message) }
        engine.onFailure = { [weak self] message in
            self?.ready = false
            self?.opening = false
            self?.engineFailed = true
            self?.operations = []
            self?.submitting = []
            self?.downloadsRunning = false
            self?.error = message
        }
        if let path = ProcessInfo.processInfo.environment["THREADGOON_WORKSPACE"] {
            openWorkspace(URL(fileURLWithPath: path))
        } else if let path = UserDefaults.standard.string(forKey: "workspacePath") {
            let url = URL(fileURLWithPath: path)
            if FileManager.default.fileExists(atPath: url.path) { openWorkspace(url) }
        }
    }

    func chooseWorkspace() {
        guard !busy, !opening else { error = "Остановите операции перед сменой рабочей папки"; return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Выбрать папку"
        panel.message = "Threadgoon сохранит базу и настройки в .threadgoon, файлы — в output этой папки. Можно выбрать папку версии 6.0."
        let current = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        panel.directoryURL = workspace ?? (FileManager.default.fileExists(atPath: current.appendingPathComponent(".threadgoon").path) ? current : FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first)
        if panel.runModal() == .OK, let url = panel.url {
            if engine.running {
                opening = true
                engine.shutdown { [weak self] in
                    // The helper sends its reply immediately before exiting; wait for its termination.
                    self?.waitForExitThenOpen(url)
                }
            } else { openWorkspace(url) }
        }
    }

    private func waitForExitThenOpen(_ url: URL) {
        if engine.running {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.waitForExitThenOpen(url) }
        } else { openWorkspace(url) }
    }

    func openWorkspace(_ url: URL) {
        workspace = url
        ready = false
        opening = true
        engineFailed = false
        initialConfigLoaded = false
        hasSearchResults = false
        threads = []; downloads = []; watches = []; boards = []; operations = []
        selectedThreads = []; selectedDownloads = []; detail = nil; prepared = nil
        downloadsRunning = false; downloadPaused = false
        detailRequestedID = nil; pendingDetailKey = nil; submitting = []
        error = nil
        status = "Открываю рабочую папку…"
        do { try engine.launch(workspace: url) }
        catch { opening = false; engineFailed = true; self.error = error.localizedDescription }
    }

    func restart() {
        guard let workspace else { chooseWorkspace(); return }
        if engine.running {
            engine.shutdown { [weak self] in self?.waitForExitThenOpen(workspace) }
        } else { openWorkspace(workspace) }
    }

    func command(_ command: String, _ params: [String: Any] = [:],
                 completion: (([String: Any]) -> Void)? = nil) {
        guard !submitting.contains(command) else { return }
        submitting.insert(command)
        objectWillChange.send()
        engine.request(command, params) { [weak self] result in
            guard let self else { return }
            self.submitting.remove(command)
            self.objectWillChange.send()
            switch result {
            case .success(let data):
                completion?(data)
                if command == "detail", !self.operations.contains(where: { $0.group == "detail" }), let key = self.pendingDetailKey {
                    self.pendingDetailKey = nil
                    self.loadDetail(key)
                }
            case .failure(let error): self.error = error.localizedDescription; self.status = "Команда не выполнена"
            }
        }
    }

    private func receive(_ message: [String: Any]) {
        do {
            let type = message["type"] as? String
            let data = message["data"] as? [String: Any] ?? [:]
            if type == "ready" {
                try apply(Wire.decode(Snapshot.self, from: data))
                ready = true; opening = false; engineFailed = false
                status = "Готово к поиску"
                if let workspace, ProcessInfo.processInfo.environment["THREADGOON_WORKSPACE"] == nil { UserDefaults.standard.set(workspace.path, forKey: "workspacePath") }
                return
            }
            guard type == "event" else { return }
            switch message["event"] as? String {
            case "snapshot": try apply(Wire.decode(Snapshot.self, from: data))
            case "operation":
                let op = try Wire.decode(EngineOperation.self, from: data)
                if let index = operations.firstIndex(where: { $0.id == op.id }) { operations[index] = op }
                else { operations.append(op) }
                status = op.message
            case "search_result":
                let record = try Wire.decode(ThreadRecord.self, from: data["thread"] ?? [:])
                if let index = threads.firstIndex(where: { $0.id == record.id }) { threads[index] = record }
                else { threads.append(record) }
            case "download_update":
                let job = try Wire.decode(DownloadJob.self, from: data)
                if let index = downloads.firstIndex(where: { $0.id == job.id }) { downloads[index] = job }
                else { downloads.append(job) }
            case "operation_finished":
                let op = try Wire.decode(EngineOperation.self, from: data)
                operations.removeAll { $0.id == op.id }
                let result = data["result"] as? [String: Any] ?? [:]
                if op.state == "failed" {
                    error = data["error"] as? String ?? "Операция не выполнена"
                    status = "Операция не выполнена"
                }
                else if op.state == "cancelled" { status = "Операция отменена" }
                else {
                    status = "Готово"
                    switch op.group {
                    case "search":
                        threads = try Wire.decode([ThreadRecord].self, from: result["threads"] ?? [])
                        selectedThreads.formIntersection(Set(threads.map(\.id)))
                        status = "Найдено тредов: \(threads.count)"
                    case "boards": boards = try Wire.decode([Board].self, from: result["boards"] ?? [])
                    case "detail":
                        let record = try Wire.decode(ThreadRecord.self, from: result["thread"] ?? [:])
                        if record.id == detailRequestedID {
                            detail = record
                            if let index = threads.firstIndex(where: { $0.id == record.id }) { threads[index] = record }
                        }
                    case "prepare": prepared = try Wire.decode(PreparedDownload.self, from: result)
                    case "watch-add": section = .watches
                    case "import":
                        let imported = try Wire.decode([ThreadRecord].self, from: result["threads"] ?? [])
                        for record in imported {
                            if let index = threads.firstIndex(where: { $0.id == record.id }) { threads[index] = record }
                            else { threads.insert(record, at: 0) }
                        }
                        if let record = imported.first { selectedThreads = [record.id]; detail = record }
                    default: break
                    }
                }
                if op.group == "detail", let key = pendingDetailKey {
                    pendingDetailKey = nil
                    loadDetail(key)
                }
            default: break
            }
        } catch { self.error = "Не удалось прочитать ответ движка: \(error.localizedDescription)" }
    }

    private func apply(_ snapshot: Snapshot) throws {
        config = snapshot.config
        if !initialConfigLoaded {
            filters = snapshot.config
            query = snapshot.config.keywords.joined(separator: ", ")
            initialConfigLoaded = true
        }
        if !hasSearchResults { threads = snapshot.threads }
        boards = snapshot.boards
        downloads = snapshot.downloads
        watches = snapshot.watches
        downloadPaused = snapshot.download_paused
        downloadsRunning = snapshot.downloads_running
        operations = snapshot.operations
        selectedDownloads.formIntersection(Set(visibleDownloads.map(\.id)))
    }

    var filterParameters: [String: Any] {
        var values = filters.json
        let text = query.trimmingCharacters(in: .whitespaces)
        values["keywords"] = text.hasPrefix("http://") || text.hasPrefix("https://") ? [] :
            text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return values
    }

    func search() {
        guard ready, !searching else { return }
        if query.trimmingCharacters(in: .whitespaces).hasPrefix("https://") || query.trimmingCharacters(in: .whitespaces).hasPrefix("http://") {
            command("import", ["urls": [query.trimmingCharacters(in: .whitespaces)], "filters": filters.json])
            hasSearchResults = true
            return
        }
        error = nil; hasSearchResults = true; threads = []; selectedThreads = []; closeDetail()
        command("search", ["boards": filters.boards, "query": query, "exact": exact,
                           "filters": filterParameters])
    }

    func loadDetail(_ key: String) {
        detailRequestedID = key
        detail = threads.first { $0.id == key }
        if operations.contains(where: { $0.group == "detail" }) || submitting.contains("detail") {
            pendingDetailKey = key
            return
        }
        command("detail", ["key": key, "filters": filterParameters])
    }

    func closeDetail() {
        detail = nil
        detailRequestedID = nil
        pendingDetailKey = nil
    }

    func prepareDownloads() {
        command("prepare_download", ["keys": selectedThreads.sorted(), "filters": filterParameters])
    }

    func confirmDownloads() {
        guard let prepared else { return }
        command("confirm_download", ["token": prepared.token]) { [weak self] _ in
            self?.prepared = nil; self?.section = .downloads; self?.downloadFilter = .active
        }
    }

    func discardDownloads() {
        guard let prepared else { return }
        command("discard_prepared", ["token": prepared.token])
        self.prepared = nil
    }

    func addWatches() {
        command("watch_add", ["keys": selectedThreads.sorted(), "filters": filterParameters])
    }

    func downloadAction(_ action: String, ids: [Int] = []) {
        command("download_control", ["action": action, "ids": ids])
    }

    func openLocal(_ relativePath: String, reveal: Bool = false) {
        guard let workspace else { return }
        let url = workspace.appendingPathComponent(relativePath).resolvingSymlinksInPath()
        let root = workspace.resolvingSymlinksInPath().path + "/"
        guard url.path.hasPrefix(root), FileManager.default.fileExists(atPath: url.path) else {
            error = "Файл отсутствует или находится вне рабочей папки"
            return
        }
        if reveal { NSWorkspace.shared.activateFileViewerSelecting([url]) }
        else { NSWorkspace.shared.open(url) }
    }

    func showDiagnostics() {
        command("diagnostics") { [weak self] data in
            self?.diagnosticText = data["log"] as? String ?? ""; self?.showingDiagnostics = true
        }
    }
}
