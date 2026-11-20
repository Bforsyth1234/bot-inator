import AppKit
import SwiftUI

/// What the assistant knows (#134): the local knowledge index Chat can
/// search to answer questions about your mail, files, past chats and
/// meeting notes, and (#137–#141) Messages, Calendar, Notes, browser
/// history and imported AI chats, whose own controls are in
/// `KnowledgeSourceOptionsViews.swift`. Every source is off by default and stays on this Mac
/// (~/.bot-inator/knowledge.db). Turning a source off deletes what was
/// indexed from it; "Forget everything" deletes the whole index.
///
/// Routes: `GET /api/knowledge`, `PUT /api/knowledge/sources/{source}`,
/// `PUT /api/knowledge/folders`, `POST /api/knowledge/index-now`,
/// `POST /api/knowledge/forget`.

struct KnowledgeSourceStatus: Codable, Equatable, Identifiable {
    let source: String
    let label: String
    let enabled: Bool
    let cloud: Bool
    let items: Int
    let chunks: Int
    let lastIndexed: Double?
    let error: String
    // #137–#141 (absent from an older daemon).
    let description: String?
    let hasOptions: Bool?
    let optionsError: String?
    let notice: String?

    var id: String { source }

    enum CodingKeys: String, CodingKey {
        case source, label, enabled, cloud, items, chunks, error, description, notice
        case lastIndexed = "last_indexed"
        case hasOptions = "has_options"
        case optionsError = "options_error"
    }
}

struct KnowledgeStatus: Codable, Equatable {
    let sources: [KnowledgeSourceStatus]
    let folders: [String]
    let running: Bool
    let blockedReason: String?
    let vector: Bool
    let dbBytes: Int

    enum CodingKeys: String, CodingKey {
        case sources, folders, running, vector
        case blockedReason = "blocked_reason"
        case dbBytes = "db_bytes"
    }
}

private struct KnowledgeSourceUpdate: Encodable {
    var enabled: Bool?
    var cloud: Bool?
}

private struct KnowledgeFoldersUpdate: Encodable {
    let folders: [String]
}

@MainActor
final class KnowledgeIndexViewModel: ObservableObject {
    @Published private(set) var status: KnowledgeStatus?
    @Published private(set) var isBusy = false
    @Published var errorMessage: String?
    @Published var statusMessage: String?

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private func send(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        var request = try DaemonAuth.request(url, method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = (json?["detail"] as? String) ?? (json?["message"] as? String)
            throw NSError(domain: "KnowledgeIndex", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    private func apply(_ data: Data) throws {
        status = try JSONDecoder().decode(KnowledgeStatus.self, from: data)
    }

    func refresh() async {
        do {
            try apply(try await send("/api/knowledge"))
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load: \(error.localizedDescription)"
        }
    }

    private func run(_ path: String, method: String, body: Data? = nil, done: String) async {
        isBusy = true
        defer { isBusy = false }
        do {
            try apply(try await send(path, method: method, body: body))
            statusMessage = done
            errorMessage = nil
        } catch {
            errorMessage = "Couldn't save: \(error.localizedDescription)"
            await refresh()
        }
    }

    func setEnabled(_ source: KnowledgeSourceStatus, _ value: Bool) async {
        let body = try? JSONEncoder().encode(KnowledgeSourceUpdate(enabled: value))
        await run("/api/knowledge/sources/\(source.source)", method: "PUT", body: body,
                  done: value ? "\(source.label) is on. It's indexed when the Mac is plugged in or you're away, or press Index now."
                              : "\(source.label) is off. What was indexed from it is deleted.")
    }

    func setCloud(_ source: KnowledgeSourceStatus, _ value: Bool) async {
        let body = try? JSONEncoder().encode(KnowledgeSourceUpdate(cloud: value))
        await run("/api/knowledge/sources/\(source.source)", method: "PUT", body: body,
                  done: value ? "Snippets from \(source.label) may go to your cloud model. Each send is in the privacy ledger."
                              : "Snippets from \(source.label) stay on this Mac.")
    }

    func setFolders(_ folders: [String]) async {
        let body = try? JSONEncoder().encode(KnowledgeFoldersUpdate(folders: folders))
        await run("/api/knowledge/folders", method: "PUT", body: body, done: "Folders saved.")
    }

    func addFolder() async {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose a folder whose documents the assistant may search"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        var folders = status?.folders ?? []
        if !folders.contains(url.path) { folders.append(url.path) }
        await setFolders(folders)
    }

    func removeFolder(_ path: String) async {
        await setFolders((status?.folders ?? []).filter { $0 != path })
    }

    func indexNow() async {
        await run("/api/knowledge/index-now", method: "POST", done: "Indexing started.")
    }

    func forgetEverything() async {
        await run("/api/knowledge/forget", method: "POST",
                  done: "The whole index is deleted and every source is off. Your lists of things to leave out are kept.")
    }
}

/// Self-contained "What the assistant knows" section: embed with
/// `KnowledgeIndexSettingsView()`.
struct KnowledgeIndexSettingsView: View {
    @StateObject private var viewModel = KnowledgeIndexViewModel()
    @State private var confirmOff: KnowledgeSourceStatus?
    @State private var confirmForget = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Text("Lets Chat answer questions about your own things (\"when is the Acme invoice due?\") by searching an index of the sources you turn on. The index is kept only on this Mac. Turning a source off deletes what was indexed from it.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            } else if let message = viewModel.statusMessage {
                Text(message).appFont(.fine).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let status = viewModel.status {
                ForEach(status.sources) { source in
                    sourceRow(source, status: status)
                    Divider()
                }
                footer(status)
            } else {
                Text("Loading…").appFont(.secondary).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
        .confirmationDialog(
            "Turn off \(confirmOff?.label ?? "this source")?",
            isPresented: Binding(get: { confirmOff != nil }, set: { if !$0 { confirmOff = nil } })
        ) {
            Button("Turn off and delete its index", role: .destructive) {
                if let source = confirmOff { Task { await viewModel.setEnabled(source, false) } }
                confirmOff = nil
            }
            Button("Cancel", role: .cancel) { confirmOff = nil }
        } message: {
            Text(offMessage(confirmOff?.source ?? ""))
        }
        .confirmationDialog("Forget everything?", isPresented: $confirmForget) {
            Button("Forget everything", role: .destructive) {
                Task { await viewModel.forgetEverything() }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Deletes the whole index, turns every source off and puts their settings back to the defaults, except the lists of things to leave out (Messages conversations, sites, browser profiles and Notes folders), which are kept. The AI-chat import list is cleared. Your actual mail, messages, files and notes are not touched. This can't be undone.")
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass.circle")
                .foregroundStyle(.secondary)
            Text("What the assistant knows").appFont(.headline)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload")
        }
    }

    private func explanation(_ source: String) -> String {
        switch source {
        case "mail": return "Emails in the Mail app (not Sent, Drafts, Junk or Trash). Needs Full Disk Access."
        case "folders": return "Text documents, PDFs, Word and RTF files in the folders you add below."
        case "chats": return "What you and the assistant said in earlier chats."
        case "meeting_notes": return "Transcripts of meetings you recorded with meeting notes."
        case "imessages": return "Your iMessage and SMS conversations, one day of a conversation at a time. These are mostly other people's words: what they wrote to you. Needs Full Disk Access."
        default: return ""
        }
    }

    private func description(_ source: KnowledgeSourceStatus) -> String {
        let own = explanation(source.source)
        return own.isEmpty ? (source.description ?? "") : own
    }

    private func offMessage(_ source: String) -> String {
        switch source {
        case "ai_chats":
            return "Everything indexed from your imports is deleted from this Mac and the import list is cleared. Your export files are not touched."
        case "browser_history":
            return "Everything indexed from your browser history is deleted from this Mac and page text is switched off. Your look-back, excluded sites and profiles are kept. Your browsers are not touched."
        case "imessages":
            return "Everything indexed from Messages is deleted from this Mac. Your left-out conversations stay left out. Your messages are not touched."
        default:
            return "Everything indexed from it is deleted from this Mac. Your actual mail, files and notes are not touched."
        }
    }

    private func sourceRow(_ source: KnowledgeSourceStatus, status: KnowledgeStatus) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Toggle(source.label, isOn: Binding(
                get: { source.enabled },
                set: { value in
                    if value {
                        Task { await viewModel.setEnabled(source, true) }
                    } else {
                        confirmOff = source
                    }
                }
            ))
            .appFont(.body)
            .disabled(viewModel.isBusy)
            Text(description(source))
                .appFont(.fine)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if source.enabled {
                Text(detail(source))
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                if !source.error.isEmpty {
                    Label(source.error, systemImage: "exclamationmark.triangle")
                        .appFont(.fine)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let notice = source.notice, !notice.isEmpty {
                    Label(notice, systemImage: "info.circle")
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let optionsError = source.optionsError, !optionsError.isEmpty {
                    Label(optionsError, systemImage: "exclamationmark.triangle")
                        .appFont(.fine)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if source.source == "folders" {
                    folderList(status.folders)
                }
                // #137–#141: per-source controls, only while the source is on.
                if source.hasOptions == true {
                    KnowledgeSourceOptionsView(source: source.source) { await viewModel.refresh() }
                }
                Toggle("Allow snippets in cloud answers", isOn: Binding(
                    get: { source.cloud },
                    set: { value in Task { await viewModel.setCloud(source, value) } }
                ))
                .appFont(.secondary)
                .disabled(viewModel.isBusy)
                .padding(.leading, 16)
                Text("Off: when Chat uses a cloud model, nothing from \(source.label) is sent. On: the few matching snippets may be sent with your question, and each send is listed in the privacy ledger.")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 16)
            }
        }
        .padding(.vertical, 2)
    }

    private func folderList(_ folders: [String]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(folders, id: \.self) { path in
                HStack {
                    Image(systemName: "folder").foregroundStyle(.secondary)
                    Text(path).appFont(.fine).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Remove") { Task { await viewModel.removeFolder(path) } }
                        .controlSize(.small)
                        .help("Stop indexing this folder and delete what was indexed from it")
                }
            }
            Button("Add folder…") { Task { await viewModel.addFolder() } }
                .controlSize(.small)
                .disabled(viewModel.isBusy)
        }
        .padding(.leading, 16)
    }

    private func detail(_ source: KnowledgeSourceStatus) -> String {
        var parts = [source.items == 1 ? "1 item indexed" : "\(source.items) items indexed"]
        if let last = source.lastIndexed {
            let date = Date(timeIntervalSince1970: last)
            parts.append("last indexed \(date.formatted(date: .abbreviated, time: .shortened))")
        } else {
            parts.append("not indexed yet")
        }
        return parts.joined(separator: " · ")
    }

    private func footer(_ status: KnowledgeStatus) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(footerText(status))
                .appFont(.fine)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button(status.running ? "Indexing…" : "Index now") {
                    Task { await viewModel.indexNow() }
                }
                .controlSize(.small)
                .disabled(viewModel.isBusy || status.running || !status.sources.contains { $0.enabled })
                Spacer()
                Button("Forget everything", role: .destructive) { confirmForget = true }
                    .controlSize(.small)
                    .disabled(viewModel.isBusy)
            }
        }
    }

    private func footerText(_ status: KnowledgeStatus) -> String {
        var parts: [String] = []
        if status.running {
            parts.append("Indexing now.")
        } else if let reason = status.blockedReason, status.sources.contains(where: { $0.enabled }) {
            parts.append("Background indexing is \(reason).")
        }
        parts.append(status.vector ? "Search by words and meaning." : "Search by words only (the local meaning model isn't available).")
        if status.dbBytes > 0 {
            parts.append("Index size \(ByteCountFormatter.string(fromByteCount: Int64(status.dbBytes), countStyle: .file)), on this Mac.")
        }
        return parts.joined(separator: " ")
    }
}
