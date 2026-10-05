import SwiftUI
import ThreadgoonCore

struct ContentView: View {
    var registerReopen: (@escaping () -> Void) -> Void = { _ in }
    @EnvironmentObject private var model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(spacing: 0) {
            GeometryReader { geometry in
                Group {
                    if model.ready { workspaceView }
                    else { welcomeView }
                }
                .frame(width: geometry.size.width, height: geometry.size.height)
            }

            // The footer participates in layout instead of covering the action buttons.
            statusBar.fixedSize(horizontal: false, vertical: true)
        }
        .frame(minWidth: 920, minHeight: 620)
        .preferredColorScheme(model.colorScheme)
        .tint(Color(red: 0.18, green: 0.48, blue: 0.45))
        .sheet(isPresented: $model.showingBoards) { BoardPickerView() }
        .sheet(item: $model.prepared, onDismiss: { model.discardDownloads() }) { prepared in
            DownloadConfirmationView(prepared: prepared)
        }
        .sheet(isPresented: $model.showingDiagnostics) { DiagnosticsView() }
        .sheet(isPresented: $model.showingHelp) { HelpView() }
        .onAppear {
            let action = openWindow
            registerReopen { action(id: "main") }
        }
    }

    private var workspaceView: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Threadgoon").font(.title3.weight(.semibold))
                    Text("Каталог и загрузки").font(.caption).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
                    .fixedSize(horizontal: false, vertical: true)
                List(selection: $model.section) {
                    ForEach(AppModel.Section.allCases) { section in
                        Label(section.rawValue, systemImage: section.symbol).tag(section)
                    }
                }.listStyle(.sidebar)
                VStack(alignment: .leading, spacing: 6) {
                    Label("Рабочая папка", systemImage: "folder").font(.caption).foregroundStyle(.secondary)
                    Text(model.workspace?.lastPathComponent ?? "").font(.callout).lineLimit(1)
                    Button("Сменить папку…", action: model.chooseWorkspace)
                        .font(.caption).buttonStyle(.link).disabled(model.busy)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(20)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(width: 190)
            .background(.bar)
            Divider()
            Group {
                switch model.section {
                case .catalog: CatalogView()
                case .downloads: DownloadsView()
                case .watches: WatchesView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .navigationTitle(model.section.rawValue)
        }
    }

    private var welcomeView: some View {
        VStack(spacing: 18) {
            Image(systemName: "square.stack.3d.up").font(.system(size: 52, weight: .light)).foregroundStyle(.secondary)
            Text(model.engineFailed ? "Движок остановлен" : "Добро пожаловать в Threadgoon").font(.largeTitle.weight(.semibold))
            Text("Выберите папку для базы, настроек и скачанных файлов.\nМожно продолжить работу с папкой версии 6.0.")
                .multilineTextAlignment(.center).foregroundStyle(.secondary)
            if model.opening {
                ProgressView("Открываю папку…")
            } else {
                HStack {
                    Button("Выбрать рабочую папку…", action: model.chooseWorkspace).buttonStyle(.borderedProminent)
                    if model.engineFailed, model.workspace != nil {
                        Button("Перезапустить движок", action: model.restart).buttonStyle(.bordered)
                    }
                }
            }
            Text("Все рабочие данные останутся внутри выбранной папки.")
                .font(.caption).foregroundStyle(.tertiary)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(40)
    }

    private var statusBar: some View {
        VStack(spacing: 0) {
            Divider()
            if let error = model.error {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(.red)
                    Text(error).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 10)
                    Button { model.error = nil } label: { Image(systemName: "xmark") }
                        .buttonStyle(.plain).help("Закрыть сообщение").accessibilityLabel("Закрыть сообщение")
                }.padding(.horizontal, 20).padding(.vertical, 10)
            }
            HStack(spacing: 8) {
                if !model.operations.isEmpty || model.opening { ProgressView().controlSize(.small) }
                Text(model.status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                if let operation = model.operations.first(where: { $0.group != "search" && $0.state != "cancelling" }) {
                    Button("Отменить") { model.command("cancel_operation", ["operation_id": operation.id]) }
                        .font(.caption).buttonStyle(.link)
                }
                Spacer()
                if model.ready {
                    Text("\(model.downloads.filter { $0.status == "downloading" }.count) загрузок · \(model.watches.filter { $0.status == "active" }.count) наблюдений")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 20).padding(.vertical, 8)
        }.background(.bar)
    }
}

struct CatalogView: View {
    @EnvironmentObject private var model: AppModel
    @State private var sort = "recent"
    private var sorted: [ThreadRecord] {
        model.threads.sorted {
            switch sort {
            case "files": return $0.file_count == $1.file_count ? $0.id < $1.id : $0.file_count > $1.file_count
            case "title": return $0.title.localizedStandardCompare($1.title) == .orderedAscending
            default: return $0.thread_id == $1.thread_id ? $0.board < $1.board : $0.thread_id > $1.thread_id
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Button { model.showingBoards = true } label: {
                        Label("Доски: \(model.filters.boards.count)", systemImage: "line.3.horizontal.decrease")
                    }.disabled(model.searching)
                    TextField("Название, описание или ссылка на тред", text: $model.query)
                        .textFieldStyle(.roundedBorder).onSubmit { model.search() }
                        .accessibilityLabel("Поиск тредов")
                    Button("Найти", action: model.search).buttonStyle(.borderedProminent)
                        .keyboardShortcut(.return, modifiers: .command)
                        .disabled(model.searching || model.filters.boards.isEmpty)
                }
                HStack {
                    Button { withAnimation(.easeInOut(duration: 0.15)) { model.filtersVisible.toggle() } } label: {
                        Label("Фильтры", systemImage: model.filtersVisible ? "chevron.up" : "chevron.down")
                    }.buttonStyle(.plain).font(.callout).disabled(model.searching)
                    Text(model.exact ? "Точный поиск по постам и файлам" : "Быстрый поиск по названию и описанию каталога")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Picker("Порядок", selection: $sort) {
                        Text("Новые сначала").tag("recent")
                        Text("Больше файлов").tag("files")
                        Text("По названию").tag("title")
                    }.labelsHidden().frame(width: 160)
                }
                if model.filtersVisible { FiltersView().disabled(model.searching) }
                if let operation = model.searchOperation {
                    HStack(spacing: 12) {
                        if operation.total > 0 {
                            ProgressView(value: Double(operation.current), total: Double(operation.total)).frame(maxWidth: 200)
                        }
                        Text("\(operation.current)/\(operation.total)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        Spacer()
                        Button(operation.paused ? "Продолжить" : "Пауза") {
                            model.command(operation.paused ? "resume_operation" : "pause_operation", ["operation_id": operation.id])
                        }.disabled(operation.state == "cancelling")
                        Button("Отменить") { model.command("cancel_operation", ["operation_id": operation.id]) }
                            .disabled(operation.state == "cancelling")
                    }
                }
            }.padding(20).fixedSize(horizontal: false, vertical: true)
            Divider()
            HStack(spacing: 0) {
                Group {
                    if model.threads.isEmpty {
                        EmptyState(symbol: model.searching ? "magnifyingglass" : "square.stack",
                                   title: model.searching ? "Ищу треды…" : "Тредов пока нет",
                                   detail: model.searching ? "Результаты точного поиска появятся по мере проверки." : "Выберите доски и нажмите «Найти». Измените запрос, если поиск ничего не нашёл.")
                    } else {
                        List(selection: $model.selectedThreads) {
                            ForEach(sorted) { record in ThreadRow(record: record).tag(record.id) }
                        }.listStyle(.inset)
                        .onChange(of: model.selectedThreads) { _, keys in
                            if keys.count == 1, let key = keys.first { model.loadDetail(key) }
                        }
                    }
                }.frame(minWidth: 340, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                if let detail = model.detail {
                    Divider()
                    ThreadDetailsView(record: detail)
                        .frame(minWidth: 280, idealWidth: 320, maxWidth: 390, minHeight: 0, maxHeight: .infinity)
                }
            }.frame(minHeight: 0, maxHeight: .infinity)
            if !model.selectedThreads.isEmpty {
                Divider()
                HStack(spacing: 12) {
                    Text("Выбрано: \(model.selectedThreads.count)").font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Button("Наблюдать", action: model.addWatches).disabled(model.preparing)
                    Button("Скачать выбранное", action: model.prepareDownloads)
                        .buttonStyle(.borderedProminent).disabled(model.preparing || model.prepared != nil)
                }.padding(.horizontal, 20).padding(.vertical, 12)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct ThreadRow: View {
    @EnvironmentObject private var model: AppModel
    let record: ThreadRecord
    var body: some View {
        HStack(spacing: 12) {
            ThumbnailView(source: record.thumbnail_url, workspace: model.workspace)
            VStack(alignment: .leading, spacing: 5) {
                Text(record.title).font(.body.weight(.medium)).lineLimit(2)
                Text("/\(record.board)/ · \(record.thread_id) · \(record.file_count) файлов")
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                if !record.snippet.isEmpty { Text(record.snippet).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
            Spacer(minLength: 6)
            VStack(alignment: .trailing, spacing: 4) {
                Text(sizeText(record.total_size)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                if record.stale { Label("Сохранённые данные", systemImage: "wifi.slash").font(.caption2).foregroundStyle(.orange) }
                else if record.accuracy == "estimated" { Text("Оценка каталога").font(.caption2).foregroundStyle(.tertiary) }
            }
        }.padding(.vertical, 7)
        .contextMenu {
            Button("Подробнее") { model.loadDetail(record.id) }
            Button("Открыть тред в браузере") {
                if let url = URL(string: record.source_url) { NSWorkspace.shared.open(url) }
            }
        }
    }
}

struct ThreadDetailsView: View {
    @EnvironmentObject private var model: AppModel
    let record: ThreadRecord
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Подробности").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                    Spacer()
                    Button(action: model.closeDetail) { Image(systemName: "xmark") }
                        .buttonStyle(.plain).help("Закрыть подробности").accessibilityLabel("Закрыть подробности")
                }
                ThumbnailView(source: record.thumbnail_url, workspace: model.workspace, size: 96)
                Text(record.title).font(.title3.weight(.semibold)).textSelection(.enabled)
                Text("/\(record.board)/ · \(record.thread_id)").font(.callout.monospacedDigit()).foregroundStyle(.secondary)
                if model.operations.contains(where: { $0.group == "detail" }) { ProgressView("Уточняю вложения…").controlSize(.small) }
                if record.stale { Label("Нет связи: сохранённые данные", systemImage: "wifi.slash").font(.caption).foregroundStyle(.orange) }
                Text("\(record.file_count) файлов · \(sizeText(record.total_size))").font(.callout)
                Text(statusText(record.status)).font(.caption).foregroundStyle(.secondary)
                if !record.snippet.isEmpty { Text(record.snippet).font(.callout).textSelection(.enabled) }
                if let url = URL(string: record.source_url) { Link("Открыть тред в браузере ↗", destination: url).font(.callout) }
                Divider()
                Text("Вложения").font(.headline)
                if record.attachments.isEmpty {
                    Text(record.accuracy == "exact" ? "Нет файлов, подходящих под текущие фильтры." : "Подождите, пока загрузятся точные данные.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(record.attachments) { attachment in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(attachment.display_name + attachment.ext).font(.callout).lineLimit(2)
                            Text(sizeText(attachment.size)).font(.caption).foregroundStyle(.secondary)
                            if let path = attachment.local_path {
                                HStack {
                                    Button("Открыть") { model.openLocal(path) }
                                    Button("В Finder") { model.openLocal(path, reveal: true) }
                                }.buttonStyle(.link).font(.caption)
                            }
                        }
                        Divider()
                    }
                }
            }.padding(20)
        }.background(.background)
    }
}

struct FiltersView: View {
    @EnvironmentObject private var model: AppModel
    @State private var minSize = ""
    @State private var maxSize = ""
    private let extensions = [".jpg", ".jpeg", ".png", ".gif", ".webm", ".mp4"]
    private func updateSize(_ value: String, key: WritableKeyPath<AppConfig, Int64?>) {
        if value.isEmpty { model.filters[keyPath: key] = nil }
        else if let mb = Double(value.replacingOccurrences(of: ",", with: ".")), mb.isFinite, mb >= 0, mb < Double(Int64.max) / 1_000_000 {
            model.filters[keyPath: key] = Int64(mb * 1_000_000)
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Toggle("Точный поиск", isOn: $model.exact).toggleStyle(.switch).controlSize(.small)
            Text(model.exact ? "Проверяет каждый тред выбранных досок. Это занимает больше времени; поиск можно приостановить." : "Типы, размеры файлов и область ключевых слов применяются при уточнении и скачивании. Включите точный поиск, чтобы отфильтровать сам каталог.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 12) {
                Text("Файлы").font(.caption).foregroundStyle(.secondary)
                ForEach(extensions, id: \.self) { ext in
                    Toggle(ext.dropFirst().uppercased(), isOn: Binding(
                        get: { model.filters.extensions.contains(ext) },
                        set: { chosen in
                            if chosen { model.filters.extensions.append(ext) }
                            else { model.filters.extensions.removeAll { $0 == ext } }
                        }))
                    .toggleStyle(.checkbox).font(.caption)
                }
            }
            HStack(spacing: 12) {
                TextField("Мин. MB", text: $minSize).frame(width: 90)
                    .onChange(of: minSize) { _, value in updateSize(value, key: \.min_file_size) }
                TextField("Макс. MB", text: $maxSize).frame(width: 90)
                    .onChange(of: maxSize) { _, value in updateSize(value, key: \.max_file_size) }
                Picker("Ключевые слова", selection: $model.filters.keyword_scope) {
                    Text("В посте с файлом").tag("file_post")
                    Text("Во всём треде").tag("whole_thread")
                }.frame(maxWidth: 300)
                Spacer()
                Button("Сбросить") {
                    let selectedBoards = model.filters.boards
                    model.filters = AppConfig(); model.filters.boards = selectedBoards; model.exact = false
                    minSize = ""; maxSize = ""
                }.buttonStyle(.link).font(.caption)
            }.textFieldStyle(.roundedBorder)
        }.padding(14).background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
        .onAppear {
            minSize = model.filters.min_file_size.map { String(Double($0) / 1_000_000) } ?? ""
            maxSize = model.filters.max_file_size.map { String(Double($0) / 1_000_000) } ?? ""
        }
    }
}

struct EmptyState: View {
    let symbol: String
    let title: String
    let detail: String
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol).font(.system(size: 36, weight: .light)).foregroundStyle(.tertiary)
            Text(title).font(.title3.weight(.medium))
            Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 380)
        }.padding(30).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
