import AppKit
import CryptoKit
import SwiftUI

actor ThumbnailLoader {
    static let shared = ThumbnailLoader()
    private var active = 0
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.timeoutIntervalForRequest = 12
        config.timeoutIntervalForResource = 20
        return URLSession(configuration: config)
    }()

    func data(url: URL, workspace: URL) async throws -> Data {
        guard url.scheme == "https", url.host == "i.4cdn.org", url.path.hasSuffix("s.jpg") else {
            throw URLError(.unsupportedURL)
        }
        let root = workspace.resolvingSymlinksInPath()
        let directory = root.appendingPathComponent(".threadgoon/cache/thumbnails").resolvingSymlinksInPath()
        guard directory.path.hasPrefix(root.path + "/") else { throw URLError(.noPermissionsToReadFile) }
        let digest = SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
        let file = directory.appendingPathComponent(digest + ".jpg").resolvingSymlinksInPath()
        guard file.path.hasPrefix(directory.path + "/") else { throw URLError(.noPermissionsToReadFile) }
        if let bytes = try? Data(contentsOf: file) { return bytes }
        while active >= 4 { try await Task.sleep(for: .milliseconds(80)) }
        try Task.checkCancellation()
        active += 1
        defer { active -= 1 }
        if let bytes = try? Data(contentsOf: file) { return bytes }
        let (bytes, response) = try await session.data(from: url)
        guard (response as? HTTPURLResponse)?.statusCode == 200, bytes.count <= 2 * 1024 * 1024 else {
            throw URLError(.badServerResponse)
        }
        try Task.checkCancellation()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try bytes.write(to: file, options: .atomic)
        return bytes
    }
}

struct ThumbnailView: View {
    let source: String
    let workspace: URL?
    var size: CGFloat = 52
    @State private var image: NSImage?
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 7).fill(.quaternary.opacity(0.55))
            if let image { Image(nsImage: image).resizable().scaledToFill() }
            else { Image(systemName: "photo").font(.title3).foregroundStyle(.tertiary) }
        }
        .frame(width: size, height: size).clipShape(RoundedRectangle(cornerRadius: 7))
        .accessibilityLabel(image == nil ? "Миниатюра недоступна" : "Миниатюра треда")
        .task(id: source + (workspace?.path ?? "")) {
            image = nil
            guard let url = URL(string: source), let workspace else { return }
            do {
                let bytes = try await ThumbnailLoader.shared.data(url: url, workspace: workspace)
                try Task.checkCancellation()
                image = NSImage(data: bytes)
            } catch { /* Keep the placeholder offline or when a row leaves the screen. */ }
        }
    }
}
