import AppKit
import SwiftUI
import ThreadgoonCore

struct DownloadsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var approving: [DownloadJob] = []
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Picker("Показать", selection: $model.downloadFilter) {
                    ForEach(DownloadFilter.allCases) { Text($0.rawValue).tag($0) }
                }.pickerStyle(.segmented).frame(maxWidth: 410)
                Spacer()
                if model.downloads.contains(where: { $0.status == "failed" }) {
                    Button("Повторить ошибки") {
                        model.downloadAction("retry", ids: model.selectedJobs.filter { $0.status == "failed" }.map(\.id))
                    }
                }
                if model.downloads.contains(where: { ["pending", "downloading", "paused"].contains($0.status) }) {
                    Button(model.downloadPaused || !model.downloadsRunning ? "Продолжить" : "Пауза") {
                        model.downloadAction(model.downloadPaused || !model.downloadsRunning ? "resume" : "pause")
                    }
                }
            }.padding(20)
            Divider()
            if model.visibleDownloads.isEmpty {
                EmptyState(symbol: "arrow.down.circle", title: "Здесь пока нет загрузок",
                           detail: "Выберите треды в каталоге и нажмите «Скачать выбранное» либо переключите фильтр списка.")
            } else {
                List(selection: $model.selectedDownloads) {
                    ForEach(model.visibleDownloads) { job in
                        DownloadRow(job: job).tag(job.id)
                    }
                }.listStyle(.inset)
            }
            if !model.selectedJobs.isEmpty {
                Divider()
                HStack(spacing: 12) {
                    Text("Выбрано: \(model.selectedJobs.count)").font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    if model.selectedJobs.contains(where: { $0.status == "waiting" }) {
                        Button("Скачать ожидающие") { approving = model.selectedJobs.filter { $0.status == "waiting" } }
                            .buttonStyle(.borderedProminent)
                    }
                    if model.selectedJobs.contains(where: { $0.canCancel }) {
                        Button("Отменить выбранные") { model.downloadAction("cancel", ids: model.selectedJobs.filter(\.canCancel).map(\.id)) }
                    }
                    if model.selectedJobs.count == 1, let job = model.selectedJobs.first, job.status == "completed" {
                        Button("Открыть") { model.openLocal(job.destination) }
                        Button("Показать в Finder") { model.openLocal(job.destination, reveal: true) }
                    }
                }.padding(.horizontal, 20).padding(.vertical, 12)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onChange(of: model.downloadFilter) { _, _ in
            model.selectedDownloads.formIntersection(Set(model.visibleDownloads.map(\.id)))
        }
        .confirmationDialog("Скачать ожидающие файлы?", isPresented: Binding(get: { !approving.isEmpty }, set: { if !$0 { approving = [] } })) {
            Button("Скачать") { model.downloadAction("approve", ids: approving.map(\.id)); approving = [] }
            Button("Отмена", role: .cancel) { approving = [] }
        } message: {
            Text("Файлы: \(approving.count) · \(sizeText(approving.allSatisfy { $0.expected_size != nil } ? approving.reduce(Int64(0)) { $0 + ($1.expected_size ?? 0) } : nil))")
        }
    }
}

struct DownloadRow: View {
    @EnvironmentObject private var model: AppModel
    let job: DownloadJob
    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: job.status == "completed" ? "checkmark.circle" : job.status == "failed" ? "exclamationmark.circle" : "doc")
                .font(.title2.weight(.light)).foregroundStyle(job.status == "failed" ? .red : .secondary).frame(width: 28)
            VStack(alignment: .leading, spacing: 7) {
                Text(job.filename).font(.body.weight(.medium)).lineLimit(1)
                Text("/\(job.board)/ · \(job.thread_id) · \(sizeText(job.downloaded_size)) из \(sizeText(job.expected_size))")
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                if job.status == "downloading", let progress = job.progress { ProgressView(value: progress).frame(maxWidth: 440) }
                if let error = job.error, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            }
            Spacer()
            Text(statusText(job.status)).font(.caption).foregroundStyle(.secondary)
        }.padding(.vertical, 9)
        .contextMenu {
            if job.status == "completed" {
                Button("Открыть") { model.openLocal(job.destination) }
                Button("Показать в Finder") { model.openLocal(job.destination, reveal: true) }
            }
            if job.canCancel { Button("Отменить") { model.downloadAction("cancel", ids: [job.id]) } }
            if ["failed", "cancelled"].contains(job.status) { Button("Повторить") { model.downloadAction("retry", ids: [job.id]) } }
        }
    }
}

struct WatchesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var removing: WatchRecord?
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Проверяем новые файлы, пока Threadgoon запущен. Автозагрузку можно включить отдельно для каждого треда.")
                .font(.callout).foregroundStyle(.secondary).padding(20)
            Divider()
            if model.watches.isEmpty {
                EmptyState(symbol: "eye", title: "Нет наблюдаемых тредов", detail: "Выберите треды в каталоге и нажмите «Наблюдать».")
            } else {
                ScrollView {
                    LazyVStack(spacing: 0) {
                    ForEach(model.watches) { watch in
                        HStack(alignment: .top, spacing: 16) {
                            Image(systemName: "eye").font(.title2.weight(.light)).foregroundStyle(.secondary)
                            VStack(alignment: .leading, spacing: 7) {
                                Text("/\(watch.board)/ · \(watch.thread_id)").font(.body.weight(.medium))
                                Text(statusText(watch.status) + " · каждые \(watch.interval) сек.").font(.caption).foregroundStyle(.secondary)
                                if let timestamp = watch.last_checked {
                                    Text("Проверено: " + Date(timeIntervalSince1970: timestamp).formatted(date: .abbreviated, time: .shortened))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                if let error = watch.last_error { Text(error).font(.caption).foregroundStyle(.red) }
                            }
                            Spacer()
                            Toggle("Автозагрузка", isOn: Binding(get: { watch.auto_download }, set: { value in
                                model.command("watch_update", ["key": watch.id, "auto_download": value])
                            })).toggleStyle(.switch).controlSize(.small).frame(width: 150)
                            Button(watch.status == "paused" ? "Продолжить" : watch.status == "unavailable" ? "Проверить снова" : "Пауза") {
                                model.command("watch_update", ["key": watch.id, "status": watch.status == "active" ? "paused" : "active"])
                            }
                            Button { removing = watch } label: { Image(systemName: "trash") }
                                .help("Удалить наблюдение").accessibilityLabel("Удалить наблюдение")
                        }.padding(.vertical, 10)
                            .buttonStyle(.borderless)
                            .accessibilityElement(children: .contain)
                        Divider()
                    }
                    }.padding(.horizontal, 20)
                }
            }
        }
        .confirmationDialog("Удалить наблюдение?", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("Удалить", role: .destructive) {
                if let removing { model.command("watch_remove", ["key": removing.id]) }
                removing = nil
            }
            Button("Отмена", role: .cancel) { removing = nil }
        } message: { Text("Скачанные файлы сохранятся.") }
    }
}

struct DownloadConfirmationView: View {
    @EnvironmentObject private var model: AppModel
    let prepared: PreparedDownload
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Скачать выбранные файлы?").font(.title2.weight(.semibold))
            Text("Точный состав проверен по текущим фильтрам.").foregroundStyle(.secondary)
            HStack(spacing: 28) {
                value("Треды", "\(prepared.thread_count)")
                value("Файлы", "\(prepared.file_count)")
                value("Объём", sizeText(prepared.total_size))
            }.padding(.vertical, 6)
            if !prepared.warnings.isEmpty {
                ScrollView { Text(prepared.warnings.joined(separator: "\n")).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                    .frame(maxHeight: 100)
            }
            Text("Папка: \(model.config.output_dir)").font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Отмена", action: model.discardDownloads).keyboardShortcut(.cancelAction)
                Button("Скачать", action: model.confirmDownloads).buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
            }
        }.padding(28).frame(width: 480)
    }
    private func value(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(text).font(.title3.weight(.medium)).monospacedDigit()
        }
    }
}
