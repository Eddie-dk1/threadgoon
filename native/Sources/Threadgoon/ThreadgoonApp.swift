import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?
    var showMainWindow: (() -> Void)?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model, model.engine.running else { return .terminateNow }
        model.status = "Завершаю операции и сохраняю данные…"
        model.engine.shutdown { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { showMainWindow?() }
        return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}

struct MainWindowCommands: Commands {
    @Environment(\.openWindow) private var openWindow
    var body: some Commands {
        CommandGroup(after: .windowArrangement) {
            Button("Главное окно Threadgoon") { openWindow(id: "main") }
                .keyboardShortcut("1", modifiers: .command)
        }
    }
}

@main
struct ThreadgoonDesktopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()
    var body: some Scene {
        Window("Threadgoon", id: "main") {
            ContentView(registerReopen: { delegate.showMainWindow = $0 })
                .environmentObject(model).onAppear { delegate.model = model }
        }
        .defaultSize(width: 1180, height: 780)
        .commands {
            MainWindowCommands()
            CommandGroup(replacing: .newItem) { }
            CommandGroup(after: .appInfo) {
                Button("Сменить рабочую папку…", action: model.chooseWorkspace).disabled(model.busy || model.opening)
                Button("Перезапустить движок", action: model.restart).disabled(!model.engineFailed)
            }
            CommandGroup(replacing: .help) {
                Button("Справка Threadgoon") { model.showingHelp = true }
                Button("Диагностика…", action: model.showDiagnostics).disabled(!model.ready)
            }
        }
        Settings { SettingsView().environmentObject(model) }
    }
}
