import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// Per-source controls for the knowledge index sources added in #137–#141
/// (Messages, Calendar, Notes, Browser history, Imported AI chats). Shown
/// by `KnowledgeIndexSettingsView` under a source only while it is on.
///
/// Routes: `GET/PUT /api/knowledge/sources/{source}/options`,
/// `GET /api/knowledge/sources/{source}/choices`,
/// `POST /api/knowledge/sources/ai_chats/imports`,
/// `DELETE /api/knowledge/sources/ai_chats/imports/{id}`.

enum KnowledgeSourceAPI {
    static let baseURL = URL(string: "http://127.0.0.1:8000")!

    static func send(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw URLError(.badURL)
        }
        var request = try DaemonAuth.request(url, method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let detail = (json?["detail"] as? String) ?? (json?["message"] as? String)
            throw NSError(domain: "KnowledgeSource", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    static func get<T: Decodable>(_ path: String, as type: T.Type) async throws -> T {
        try JSONDecoder().decode(T.self, from: try await send(path))
    }

    static func options<T: Decodable>(_ source: String, as type: T.Type) async throws -> T {
        try await get("/api/knowledge/sources/\(source)/options", as: OptionsEnvelope<T>.self).options
    }

    static func putOptions<T: Encodable>(_ source: String, _ options: T) async throws {
        _ = try await send("/api/knowledge/sources/\(source)/options", method: "PUT",
                           body: try JSONEncoder().encode(options))
    }

    static func choices<T: Decodable>(_ source: String, as type: T.Type) async throws -> T {
        try await get("/api/knowledge/sources/\(source)/choices", as: T.self)
    }
}

private struct OptionsEnvelope<T: Decodable>: Decodable {
    let options: T
}

/// Picks the right controls for `source`; nothing for the four #134 sources.
struct KnowledgeSourceOptionsView: View {
    let source: String
    /// Called after a save so the parent reloads counts and errors.
    let onChange: () async -> Void

    var body: some View {
        Group {
            switch source {
            case "imessages": IMessageOptionsView(onChange: onChange)
            case "calendar": CalendarOptionsView(onChange: onChange)
            case "apple_notes": NotesOptionsView(onChange: onChange)
            case "browser_history": BrowserOptionsView(onChange: onChange)
            case "ai_chats": AIChatImportsView(onChange: onChange)
            default: EmptyView()
            }
        }
        .padding(.leading, 16)
    }
}

/// A small error / status line used by every options view.
private struct OptionsMessage: View {
    let error: String?
    var body: some View {
        if let error, !error.isEmpty {
            Label(error, systemImage: "exclamationmark.triangle")
                .appFont(.fine)
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Messages (#137)

private struct IMessageOptions: Codable { var exclude: [String] }

private struct IMessageChoices: Decodable {
    struct Conversation: Decodable, Identifiable {
        let id: String
        let title: String
        let isGroup: Bool
        let lastMessage: String
        let excluded: Bool
        enum CodingKeys: String, CodingKey {
            case id, title, excluded
            case isGroup = "is_group"
            case lastMessage = "last_message"
        }
    }
    let conversations: [Conversation]
    let error: String
}

struct IMessageOptionsView: View {
    let onChange: () async -> Void
    @State private var options = IMessageOptions(exclude: [])
    @State private var choices: IMessageChoices?
    @State private var error: String?
    @State private var busy = false
    @State private var confirmExclude: IMessageChoices.Conversation?
    @State private var filter = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Leave out conversations you don't want searched. Leaving one out deletes what was indexed from it right away. The conversation with your iPhone remote is always left out.")
                .appFont(.fine).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let choices, !choices.error.isEmpty {
                OptionsMessage(error: choices.error)  // e.g. no Full Disk Access
            }
            OptionsMessage(error: error)
            if let conversations = choices?.conversations, !conversations.isEmpty {
                TextField("Find a conversation", text: $filter)
                    .textFieldStyle(.roundedBorder)
                    .appFont(.fine)
                    .frame(maxWidth: 260)
                ScrollView {
                    // Lazy: a Mac can have thousands of conversations, and
                    // a checkbox per one built up front cost ~800 MB and
                    // made every window focus change redraw them all.
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(shown(conversations)) { conv in
                            Toggle(isOn: Binding(
                                get: { !isExcluded(conv) },
                                set: { keep in
                                    if keep { Task { await save(options.exclude.filter { $0 != conv.id }) } }
                                    else { confirmExclude = conv }
                                }
                            )) {
                                HStack(spacing: 4) {
                                    Image(systemName: conv.isGroup ? "person.3" : "person")
                                        .foregroundStyle(.secondary)
                                    Text(conv.title).appFont(.fine).lineLimit(1)
                                }
                            }
                            .toggleStyle(.checkbox)
                            .disabled(busy)
                        }
                    }
                }
                .frame(maxHeight: 160)
            }
            if !options.exclude.isEmpty {
                Text(options.exclude.count == 1 ? "1 conversation left out." : "\(options.exclude.count) conversations left out.")
                    .appFont(.fine).foregroundStyle(.secondary)
            }
        }
        .task { await load() }
        .confirmationDialog(
            "Leave out \(confirmExclude?.title ?? "this conversation")?",
            isPresented: Binding(get: { confirmExclude != nil }, set: { if !$0 { confirmExclude = nil } })
        ) {
            Button("Leave out and delete its index", role: .destructive) {
                if let conv = confirmExclude { Task { await save(options.exclude + [conv.id]) } }
                confirmExclude = nil
            }
            Button("Cancel", role: .cancel) { confirmExclude = nil }
        } message: {
            Text("What was indexed from this conversation is deleted from this Mac now and it is never indexed again. Your messages themselves are not touched.")
        }
    }

    private func isExcluded(_ conv: IMessageChoices.Conversation) -> Bool {
        options.exclude.contains(conv.id) || conv.excluded
    }

    private func shown(_ all: [IMessageChoices.Conversation]) -> [IMessageChoices.Conversation] {
        let q = filter.trimmingCharacters(in: .whitespaces).lowercased()
        return q.isEmpty ? all : all.filter { $0.title.lowercased().contains(q) }
    }

    private func load() async {
        do {
            options = try await KnowledgeSourceAPI.options("imessages", as: IMessageOptions.self)
            choices = try await KnowledgeSourceAPI.choices("imessages", as: IMessageChoices.self)
            error = nil
        } catch {
            self.error = "Couldn't load conversations: \(error.localizedDescription)"
        }
    }

    private func save(_ exclude: [String]) async {
        busy = true
        defer { busy = false }
        do {
            try await KnowledgeSourceAPI.putOptions("imessages", IMessageOptions(exclude: exclude))
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
    }
}

// MARK: - Calendar (#138)

private struct CalendarOptions: Codable, Equatable {
    var daysBack: Int
    var daysAhead: Int
    enum CodingKeys: String, CodingKey {
        case daysBack = "days_back"
        case daysAhead = "days_ahead"
    }
}

struct CalendarOptionsView: View {
    let onChange: () async -> Void
    @State private var saved: CalendarOptions?
    @State private var draft = CalendarOptions(daysBack: 730, daysAhead: 365)
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Read through the menu bar app's Calendar access (the Permissions screen). Events in this window are indexed; shortening it deletes events outside it.")
                .appFont(.fine).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Stepper("Days back: \(draft.daysBack)", value: $draft.daysBack, in: 0...3650, step: 30)
                .appFont(.fine)
            Stepper("Days ahead: \(draft.daysAhead)", value: $draft.daysAhead, in: 0...1825, step: 30)
                .appFont(.fine)
            OptionsMessage(error: error)
            if let saved, saved != draft {
                HStack {
                    Button("Save window") { Task { await save() } }.controlSize(.small)
                    Button("Revert") { draft = saved }.controlSize(.small)
                }
            }
        }
        .task { await load() }
    }

    private func load() async {
        do {
            let got = try await KnowledgeSourceAPI.options("calendar", as: CalendarOptions.self)
            saved = got
            draft = got
            error = nil
        } catch {
            self.error = "Couldn't load: \(error.localizedDescription)"
        }
    }

    private func save() async {
        do {
            try await KnowledgeSourceAPI.putOptions("calendar", draft)
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
    }
}

// MARK: - Notes (#139)

private struct NotesOptions: Codable {
    var excludeFolders: [String]
    enum CodingKeys: String, CodingKey { case excludeFolders = "exclude_folders" }
}

private struct NotesChoices: Decodable {
    struct Folder: Decodable, Identifiable {
        let path: String
        let notes: Int
        var id: String { path }
    }
    let folders: [Folder]
    let error: String
}

struct NotesOptionsView: View {
    let onChange: () async -> Void
    @State private var options = NotesOptions(excludeFolders: [])
    @State private var choices: NotesChoices?
    @State private var error: String?
    @State private var busy = false
    @State private var confirmExclude: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Untick a folder to leave it out; what was indexed from it is deleted right away. Locked notes and Recently Deleted are never read.")
                .appFont(.fine).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let choices, !choices.error.isEmpty { OptionsMessage(error: choices.error) }
            OptionsMessage(error: error)
            ForEach(choices?.folders ?? []) { folder in
                Toggle(isOn: Binding(
                    get: { !options.excludeFolders.contains(folder.path) },
                    set: { keep in
                        if keep {
                            Task { await save(options.excludeFolders.filter { $0 != folder.path }) }
                        } else {
                            confirmExclude = folder.path
                        }
                    }
                )) {
                    Text("\(folder.path) (\(folder.notes))").appFont(.fine).lineLimit(1)
                }
                .toggleStyle(.checkbox)
                .disabled(busy)
            }
            // Excluded folders no longer listed by Notes (renamed or gone).
            ForEach(options.excludeFolders.filter { path in
                !(choices?.folders ?? []).contains { $0.path == path }
            }, id: \.self) { path in
                HStack {
                    Text("Left out: \(path)").appFont(.fine).foregroundStyle(.secondary)
                    Button("Include again") {
                        Task { await save(options.excludeFolders.filter { $0 != path }) }
                    }.controlSize(.small)
                }
            }
        }
        .task { await load() }
        .confirmationDialog(
            "Leave out \(confirmExclude ?? "this folder")?",
            isPresented: Binding(get: { confirmExclude != nil }, set: { if !$0 { confirmExclude = nil } })
        ) {
            Button("Leave out and delete its index", role: .destructive) {
                if let path = confirmExclude { Task { await save(options.excludeFolders + [path]) } }
                confirmExclude = nil
            }
            Button("Cancel", role: .cancel) { confirmExclude = nil }
        } message: {
            Text("What was indexed from this folder is deleted from this Mac now. Your notes are not touched.")
        }
    }

    private func load() async {
        do {
            options = try await KnowledgeSourceAPI.options("apple_notes", as: NotesOptions.self)
            choices = try await KnowledgeSourceAPI.choices("apple_notes", as: NotesChoices.self)
            error = nil
        } catch {
            self.error = "Couldn't load folders: \(error.localizedDescription)"
        }
    }

    private func save(_ folders: [String]) async {
        busy = true
        defer { busy = false }
        do {
            try await KnowledgeSourceAPI.putOptions("apple_notes", NotesOptions(excludeFolders: folders))
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
    }
}

// MARK: - Browser history (#140)

private struct BrowserOptions: Codable, Equatable {
    var lookbackDays: Int
    var pageText: Bool
    var excludedDomains: [String]
    /// nil = every profile found.
    var profiles: [String]?
    enum CodingKeys: String, CodingKey {
        case lookbackDays = "lookback_days"
        case pageText = "page_text"
        case excludedDomains = "excluded_domains"
        case profiles
    }
}

private struct BrowserChoices: Decodable {
    struct Profile: Decodable, Identifiable {
        let key: String
        let name: String
        let browserTitle: String
        let readable: Bool
        let error: String
        var id: String { key }
        enum CodingKeys: String, CodingKey {
            case key, name, readable, error
            case browserTitle = "browser_title"
        }
    }
    struct Lookback: Decodable, Hashable {
        let value: Int
        let label: String
    }
    let profiles: [Profile]
    let missingProfiles: [String]
    let lookbackDays: [Lookback]
    let suggestedExcludedDomains: [String]
    enum CodingKeys: String, CodingKey {
        case profiles
        case missingProfiles = "missing_profiles"
        case lookbackDays = "lookback_days"
        case suggestedExcludedDomains = "suggested_excluded_domains"
    }
}

struct BrowserOptionsView: View {
    let onChange: () async -> Void
    @State private var options: BrowserOptions?
    @State private var choices: BrowserChoices?
    @State private var error: String?
    @State private var busy = false
    @State private var newSite = ""
    @State private var confirmPageText = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            OptionsMessage(error: error)
            if let options, let choices {
                Picker("Look back", selection: Binding(
                    get: { options.lookbackDays },
                    set: { value in var o = options; o.lookbackDays = value; Task { await save(o) } }
                )) {
                    ForEach(choices.lookbackDays, id: \.self) { Text($0.label).tag($0.value) }
                }
                .appFont(.fine)
                .frame(maxWidth: 240)
                .disabled(busy)
                Text("Only pages visited within this time are indexed. Shortening it deletes older pages from the index right away.")
                    .appFont(.fine).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Toggle("Also index page text (uses the network)", isOn: Binding(
                    get: { options.pageText },
                    set: { on in
                        if on { confirmPageText = true }
                        else { var o = options; o.pageText = false; Task { await save(o) } }
                    }
                ))
                .appFont(.fine)
                .disabled(busy)
                Text("Off: only the titles and addresses already in your browsers' history files are indexed, and nothing goes on the network. On: each visited page is downloaded again (at most once a month, without your cookies or logins, never local or private addresses) and its text indexed. This also needs web access turned on in Settings › Web search, and pages are fetched only while the Mac is on AC power or you're away; \"Index now\" indexes titles at once and leaves the downloads for then. Every download is listed in the privacy ledger.")
                    .appFont(.fine).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                Text("Sites never indexed").appFont(.secondary)
                ForEach(options.excludedDomains, id: \.self) { site in
                    HStack {
                        Text(site).appFont(.fine)
                        Spacer()
                        Button("Remove") {
                            var o = options
                            o.excludedDomains.removeAll { $0 == site }
                            Task { await save(o) }
                        }
                        .controlSize(.small)
                        .disabled(busy)
                    }
                }
                HStack {
                    TextField("example.com", text: $newSite)
                        .textFieldStyle(.roundedBorder)
                        .appFont(.fine)
                        .frame(maxWidth: 200)
                        .onSubmit { addSite(options) }
                    Button("Add site") { addSite(options) }
                        .controlSize(.small)
                        .disabled(busy || newSite.trimmingCharacters(in: .whitespaces).isEmpty)
                    let missing = choices.suggestedExcludedDomains.filter { !options.excludedDomains.contains($0) }
                    if !missing.isEmpty {
                        Button("Add suggested (\(missing.count))") {
                            var o = options
                            o.excludedDomains += missing
                            Task { await save(o) }
                        }
                        .controlSize(.small)
                        .help("Banking, payment, web mail and sign-in sites")
                        .disabled(busy)
                    }
                }
                Text("Adding a site deletes its pages from the index right away (subdomains included).")
                    .appFont(.fine).foregroundStyle(.secondary)

                Text("Browsers and profiles").appFont(.secondary)
                if choices.profiles.isEmpty {
                    Text("No browser history was found on this Mac.").appFont(.fine).foregroundStyle(.secondary)
                }
                ForEach(choices.profiles) { profile in
                    VStack(alignment: .leading, spacing: 0) {
                        Toggle(isOn: Binding(
                            get: { options.profiles?.contains(profile.key) ?? true },
                            set: { on in
                                var o = options
                                var keys = o.profiles ?? choices.profiles.map(\.key)
                                if on { if !keys.contains(profile.key) { keys.append(profile.key) } }
                                else { keys.removeAll { $0 == profile.key } }
                                o.profiles = keys
                                Task { await save(o) }
                            }
                        )) {
                            Text(profile.browserTitle == profile.name ? profile.name
                                 : "\(profile.browserTitle) (\(profile.name))").appFont(.fine)
                        }
                        .toggleStyle(.checkbox)
                        .disabled(busy)
                        if !profile.readable {
                            Text(profile.error.isEmpty ? "Can't be read." : profile.error)
                                .appFont(.fine).foregroundStyle(.orange)
                                .fixedSize(horizontal: false, vertical: true)
                                .padding(.leading, 20)
                        }
                    }
                }
                ForEach(choices.missingProfiles, id: \.self) { key in
                    Text("\(key): not found on this Mac any more").appFont(.fine).foregroundStyle(.orange)
                }
            } else if error == nil {
                Text("Loading…").appFont(.fine).foregroundStyle(.secondary)
            }
        }
        .task { await load() }
        .confirmationDialog("Also index page text?", isPresented: $confirmPageText) {
            Button("Download and index page text") {
                if var o = options { o.pageText = true; Task { await save(o) } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The assistant will download the pages in your history (public versions only, no cookies) so it can search their text. This uses the network; each download is in the privacy ledger. It needs web access on in Settings › Web search too.")
        }
    }

    private func addSite(_ options: BrowserOptions) {
        let site = newSite.trimmingCharacters(in: .whitespaces)
        guard !site.isEmpty else { return }
        var o = options
        o.excludedDomains.append(site)
        newSite = ""
        Task { await save(o) }
    }

    private func load() async {
        do {
            options = try await KnowledgeSourceAPI.options("browser_history", as: BrowserOptions.self)
            choices = try await KnowledgeSourceAPI.choices("browser_history", as: BrowserChoices.self)
            error = nil
        } catch {
            self.error = "Couldn't load: \(error.localizedDescription)"
        }
    }

    private func save(_ new: BrowserOptions) async {
        busy = true
        defer { busy = false }
        do {
            try await KnowledgeSourceAPI.putOptions("browser_history", new)
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't save: \(error.localizedDescription)"
        }
    }
}

// MARK: - Imported AI chats (#141)

private struct AIChatChoices: Decodable {
    struct Import: Decodable, Identifiable {
        let id: String
        let fileName: String
        let providerLabel: String
        let addedAt: String
        let conversations: Int
        let present: Bool
        enum CodingKeys: String, CodingKey {
            case id, conversations, present
            case fileName = "file_name"
            case providerLabel = "provider_label"
            case addedAt = "added_at"
        }
    }
    let imports: [Import]
    let error: String
}

private struct ImportBody: Encodable { let path: String }

struct AIChatImportsView: View {
    let onChange: () async -> Void
    @State private var imports: [AIChatChoices.Import] = []
    @State private var error: String?
    @State private var busy = false
    @State private var confirmRemove: AIChatChoices.Import?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Download your data export from ChatGPT or Claude, then pick its conversations.json or the .zip here. The file is read where it is and nothing is sent to OpenAI or Anthropic. Importing the same file again updates it.")
                .appFont(.fine).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            OptionsMessage(error: error)
            ForEach(imports) { item in
                HStack(spacing: 6) {
                    Image(systemName: "doc.text").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(item.fileName).appFont(.fine).lineLimit(1).truncationMode(.middle)
                        Text("\(item.providerLabel) · \(item.conversations) conversation\(item.conversations == 1 ? "" : "s") · imported \(String(item.addedAt.prefix(10)))")
                            .appFont(.fine).foregroundStyle(.secondary)
                    }
                    if !item.present {
                        Text("file missing")
                            .appFont(.fine)
                            .padding(.horizontal, 4)
                            .background(Capsule().fill(Color.orange.opacity(0.2)))
                            .help("The file was moved or deleted. Its chats stay in the index until you remove the import.")
                    }
                    Spacer()
                    Button("Remove") { confirmRemove = item }
                        .controlSize(.small)
                        .disabled(busy)
                }
            }
            Button(busy ? "Importing…" : "Import…") { Task { await pickAndImport() } }
                .controlSize(.small)
                .disabled(busy)
        }
        .task { await load() }
        .confirmationDialog(
            "Remove \(confirmRemove?.fileName ?? "this import")?",
            isPresented: Binding(get: { confirmRemove != nil }, set: { if !$0 { confirmRemove = nil } })
        ) {
            Button("Remove and delete its chats from the index", role: .destructive) {
                if let item = confirmRemove { Task { await remove(item) } }
                confirmRemove = nil
            }
            Button("Cancel", role: .cancel) { confirmRemove = nil }
        } message: {
            Text("Its conversations are deleted from this Mac's index. The export file itself is not touched.")
        }
    }

    private func load() async {
        do {
            imports = try await KnowledgeSourceAPI.choices("ai_chats", as: AIChatChoices.self).imports
            error = nil
        } catch {
            self.error = "Couldn't load imports: \(error.localizedDescription)"
        }
    }

    private func pickAndImport() async {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json, .zip]
        panel.message = "Choose a ChatGPT or Claude data export (conversations.json or the .zip)"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await KnowledgeSourceAPI.send(
                "/api/knowledge/sources/ai_chats/imports", method: "POST",
                body: try JSONEncoder().encode(ImportBody(path: url.path)))
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't import: \(error.localizedDescription)"
        }
    }

    private func remove(_ item: AIChatChoices.Import) async {
        busy = true
        defer { busy = false }
        do {
            _ = try await KnowledgeSourceAPI.send(
                "/api/knowledge/sources/ai_chats/imports/\(item.id)", method: "DELETE")
            await load()
            await onChange()
        } catch {
            self.error = "Couldn't remove: \(error.localizedDescription)"
        }
    }
}
