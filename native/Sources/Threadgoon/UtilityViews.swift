import SwiftUI
import ThreadgoonCore

struct BoardPickerView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var selected = Set<String>()
    @State private var search = ""
    private var visible: [Board] {
        model.boards.filter { search.isEmpty || "\($0.board) \($0.title)".localizedCaseInsensitiveContains(search) }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Доски для поиска").font(.title2.weight(.semibold))
                Spacer()
                Button("Обновить список") { model.command("refresh_boards") }
                    .disabled(model.operations.contains { $0.group == "boards" })
            }
            TextField("Найти доску по коду или названию", text: $search).textFieldStyle(.roundedBorder)
            if model.operations.contains(where: { $0.group == "boards" }) { ProgressView("Загружаю список досок…").controlSize(.small) }
            List {
                ForEach(visible) { board in
                    Toggle(isOn: binding(board.board)) {
                        HStack {
                            Text("/\(board.board)/").font(.body.monospaced()).frame(width: 66, alignment: .leading)
                            Text(board.title)
                            Spacer()
                            if !board.available { Text("Недоступна").font(.caption).foregroundStyle(.secondary) }
                        }
                    }.toggleStyle(.checkbox).padding(.vertical, 4)
                }
                ForEach(selected.subtracting(Set(model.boards.map(\.board))).sorted(), id: \.self) { board in
                    Toggle("/\(board)/ · сохранённая доска", isOn: binding(board)).toggleStyle(.checkbox)
                }
            }.listStyle(.inset)
            HStack {
                Text("Выбрано: \(selected.count)").foregroundStyle(.secondary)
                Spacer()
                Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Применить") {
                    model.filters.boards = selected.sorted()
                    model.command("boards_save", ["boards": selected.sorted()])
                    dismiss()
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(selected.isEmpty)
            }
        }.padding(24).frame(width: 580, height: 550)
        .onAppear {
            selected = Set(model.filters.boards)
            if model.boards.isEmpty { model.command("refresh_boards") }
        }
    }
    private func binding(_ board: String) -> Binding<Bool> {
        Binding(get: { selected.contains(board) }, set: { enabled in
            if enabled { selected.insert(board) } else { selected.remove(board) }
        })
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var draft = AppConfig()
    @State private var saveMessage = ""
    @State private var showingLog = false
    var body: some View {
        Form {
            Section("Загрузки") {
                TextField("Папка внутри рабочей папки", text: $draft.output_dir)
                Stepper("Параллельных загрузок: \(draft.media_workers)", value: $draft.media_workers, in: 1...3)
                Text("Смена папки влияет на новые задания. Уже добавленные файлы сохраняют своё расположение.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Наблюдение") {
                Stepper("Проверять каждые \(draft.watch_interval) сек.", value: $draft.watch_interval, in: 10...3600, step: 10)
                Toggle("Автозагрузка для новых наблюдений", isOn: $draft.auto_download_new)
                Text("Существующие наблюдения настраиваются в разделе «Наблюдение».").font(.caption).foregroundStyle(.secondary)
            }
            Section("Интерфейс") {
                Picker("Оформление", selection: $model.appearance) {
                    Text("Как в системе").tag("system")
                    Text("Светлое").tag("light")
                    Text("Тёмное").tag("dark")
                }
            }
            Section("Дополнительно") {
                Stepper("Интервал API: \(draft.api_interval, specifier: "%.1f") сек.", value: $draft.api_interval, in: 1...10, step: 0.5)
                HStack {
                    Button("Открыть журнал") {
                        model.command("diagnostics") { data in
                            model.diagnosticText = data["log"] as? String ?? ""
                            showingLog = true
                        }
                    }.disabled(!model.ready)
                    Button("Сменить рабочую папку…", action: model.chooseWorkspace).disabled(model.busy || model.opening)
                }
            }
            HStack {
                Text(saveMessage).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Сохранить") {
                    var config = draft
                    config.boards = model.filters.boards
                    config.extensions = model.filters.extensions
                    config.min_file_size = model.filters.min_file_size
                    config.max_file_size = model.filters.max_file_size
                    config.keyword_scope = model.filters.keyword_scope
                    config.keywords = model.query.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                    model.command("settings_save", ["config": config.json]) { _ in saveMessage = "Настройки сохранены" }
                }.buttonStyle(.borderedProminent).disabled(!model.ready || model.busy)
            }
            if model.busy { Text("Сохранение настроек доступно после завершения операций.").font(.caption).foregroundStyle(.secondary) }
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.red) }
        }.formStyle(.grouped).padding(16).frame(width: 570, height: 620)
        .preferredColorScheme(model.colorScheme)
        .onAppear { draft = model.config }
        .sheet(isPresented: $showingLog) { DiagnosticsView().environmentObject(model) }
    }
}

struct DiagnosticsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Диагностика").font(.title2.weight(.semibold))
                Spacer()
                Button("Обновить") {
                    model.command("diagnostics") { model.diagnosticText = $0["log"] as? String ?? "" }
                }
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Рабочая папка: \(model.workspace?.path ?? "—")").font(.caption).textSelection(.enabled)
            ScrollView {
                Text(model.diagnosticText).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(12)
            }.background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
        }.padding(24).frame(width: 740, height: 510)
    }
}

struct HelpView: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Как пользоваться Threadgoon").font(.title2.weight(.semibold))
            Text("1. Выберите доски и нажмите «Найти».\n2. Выберите треды. Для нескольких используйте ⌘ или ⇧.\n3. Нажмите «Скачать выбранное» и подтвердите объём.")
                .font(.body).lineSpacing(7)
            Text("Быстрый режим ищет по каталогу. Точный поиск в фильтрах проверяет посты и файлы каждого треда. Несколько ключевых слов через запятую означают совпадение хотя бы одного слова.")
            Text("Можно вставить ссылку на тред в строку поиска. Наблюдение работает, пока приложение запущено. При выходе незавершённые файлы сохраняются для докачивания.")
            Text("Все рабочие данные находятся в выбранной папке: .threadgoon — база и настройки, output — загрузки.")
                .foregroundStyle(.secondary)
            Link("Источник данных — 4chan", destination: URL(string: "https://www.4chan.org/")!)
            Text("Threadgoon — независимое приложение. Версия 6.1.0.").font(.caption).foregroundStyle(.secondary)
            HStack { Spacer(); Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction) }
        }.padding(28).frame(width: 540)
    }
}
