import SwiftUI

// Right-moment delivery (#87): offers held while the user is busy, the
// Inbox for offers over the daily cap, Pause, and the delivery settings
// (daily cap, never-watch apps). Talks to `routes/delivery.py`.

/// One held offer or Inbox entry (`GET /api/delivery`, `held` / `inbox`).
struct HeldOfferSummary: Codable, Hashable, Identifiable {
    let id: String
    let eventType: String
    let source: String
    let kind: String
    let title: String
    let toolName: String
    let reason: String
    let heldSince: String?
    let expiresAt: String?

    enum CodingKeys: String, CodingKey {
        case id, source, kind, title, reason
        case eventType = "event_type"
        case toolName = "tool_name"
        case heldSince = "held_since"
        case expiresAt = "expires_at"
    }

    var expiresDate: Date? { expiresAt.flatMap { ISO8601DateFormatter.delivery.date(from: $0) } }
}

/// `GET /api/delivery`.
struct DeliveryStatus: Codable, Hashable {
    let paused: Bool
    /// ISO 8601 deadline, `"indefinite"`, or nil when not paused.
    let pausedUntil: String?
    let heldCount: Int
    let heldReasons: [String: Int]
    let held: [HeldOfferSummary]
    let inboxCount: Int
    let inbox: [HeldOfferSummary]
    let cardsToday: Int
    let dailyCap: Int
    let checks: [String: String]
    let debugLine: String?

    enum CodingKeys: String, CodingKey {
        case paused, held, inbox, checks
        case pausedUntil = "paused_until"
        case heldCount = "held_count"
        case heldReasons = "held_reasons"
        case inboxCount = "inbox_count"
        case cardsToday = "cards_today"
        case dailyCap = "daily_cap"
        case debugLine = "debug_line"
    }

    var pausedText: String? {
        guard paused else { return nil }
        guard let until = pausedUntil, until != "indefinite",
              let date = ISO8601DateFormatter.delivery.date(from: until) else {
            return "Paused until you resume"
        }
        return "Paused until \(date.formatted(date: .omitted, time: .shortened))"
    }
}

/// `GET/PUT /api/delivery/settings`.
struct DeliverySettings: Codable, Hashable {
    var dailyCap: Int
    var neverWatch: [String]
    var neverWatchSeed: [String]?

    enum CodingKeys: String, CodingKey {
        case dailyCap = "daily_cap"
        case neverWatch = "never_watch"
        case neverWatchSeed = "never_watch_seed"
    }
}

/// Pause choices offered in the menu (`POST /api/delivery/pause`).
enum PauseDuration: String, CaseIterable, Identifiable, Codable, Hashable {
    case oneHour = "1h"
    case tomorrow
    case indefinite

    var id: String { rawValue }
    var label: String {
        switch self {
        case .oneHour: return "For 1 hour"
        case .tomorrow: return "Until tomorrow"
        case .indefinite: return "Until I resume"
        }
    }
}

extension ISO8601DateFormatter {
    /// The daemon sends whole-second times with an offset (`...T10:00:00+00:00`).
    static let delivery = ISO8601DateFormatter()
}

@MainActor
final class DeliveryViewModel: ObservableObject {
    @Published private(set) var status: DeliveryStatus?
    @Published var settings: DeliverySettings?
    @Published private(set) var isLoading = false
    @Published var errorMessage: String?

    /// Count for the menu bar icon badge.
    var inboxCount: Int { status?.inboxCount ?? 0 }
    var isPaused: Bool { status?.paused ?? false }

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private func send(_ path: String, method: String = "GET",
                      body: [String: Any]? = nil) async throws -> Data {
        var request = try DaemonAuth.request(baseURL.appendingPathComponent(path), method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw NSError(domain: "Delivery", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"])
        }
        return data
    }

    private func apply(_ data: Data) throws {
        status = try JSONDecoder().decode(DeliveryStatus.self, from: data)
        errorMessage = nil
    }

    func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            try apply(try await send("api/delivery"))
        } catch {
            errorMessage = "Failed to load held offers: \(error.localizedDescription)"
        }
    }

    func pause(_ duration: PauseDuration) async {
        do {
            try apply(try await send("api/delivery/pause", method: "POST",
                                     body: ["duration": duration.rawValue]))
        } catch {
            errorMessage = "Failed to pause: \(error.localizedDescription)"
        }
    }

    func resume() async {
        do {
            try apply(try await send("api/delivery/resume", method: "POST"))
        } catch {
            errorMessage = "Failed to resume: \(error.localizedDescription)"
        }
    }

    func open(_ offer: HeldOfferSummary) async {
        do {
            _ = try await send("api/delivery/inbox/\(offer.id)/open", method: "POST")
            await refresh()
        } catch {
            errorMessage = "Failed to show the offer: \(error.localizedDescription)"
        }
    }

    func dismiss(_ offer: HeldOfferSummary) async {
        do {
            _ = try await send("api/delivery/inbox/\(offer.id)", method: "DELETE")
            await refresh()
        } catch {
            errorMessage = "Failed to dismiss the offer: \(error.localizedDescription)"
        }
    }

    func loadSettings() async {
        do {
            settings = try JSONDecoder().decode(DeliverySettings.self,
                                                from: try await send("api/delivery/settings"))
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load delivery settings: \(error.localizedDescription)"
        }
    }

    func saveSettings(_ new: DeliverySettings) async {
        do {
            let data = try await send("api/delivery/settings", method: "PUT",
                                      body: ["daily_cap": new.dailyCap,
                                             "never_watch": new.neverWatch])
            settings = try JSONDecoder().decode(DeliverySettings.self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to save delivery settings: \(error.localizedDescription)"
        }
    }
}

/// Menu items for Pause / Resume. Embed inside the menu bar menu.
struct PauseMenu: View {
    @ObservedObject var viewModel: DeliveryViewModel

    var body: some View {
        if viewModel.isPaused {
            Button("Resume") { Task { await viewModel.resume() } }
        } else {
            Menu("Pause") {
                ForEach(PauseDuration.allCases) { duration in
                    Button(duration.label) { Task { await viewModel.pause(duration) } }
                }
            }
        }
    }
}

/// The Inbox: offers over today's cap, and offers held for a better moment.
struct OfferInboxView: View {
    @ObservedObject var viewModel: DeliveryViewModel

    init(viewModel: DeliveryViewModel) {
        self.viewModel = viewModel
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if let paused = viewModel.status?.pausedText {
                HStack {
                    Label(paused, systemImage: "pause.circle")
                        .appFont(.secondary)
                    Spacer()
                    Button("Resume") { Task { await viewModel.resume() } }
                        .controlSize(.small)
                }
            }
            section(title: "Inbox", items: viewModel.status?.inbox ?? [], showActions: true,
                    empty: "Nothing waiting. Suggestions over today's limit land here.")
            section(title: "Held for later", items: viewModel.status?.held ?? [], showActions: false,
                    empty: "Nothing held. Suggestions wait here during meetings and while you type.")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task {
            while !Task.isCancelled {
                await viewModel.refresh()
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "tray.full").foregroundStyle(.secondary)
            Text("Suggestions").appFont(.headline)
            if let s = viewModel.status {
                Text("\(s.cardsToday) of \(s.dailyCap) today")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .disabled(viewModel.isLoading)
            .help("Reload")
        }
    }

    @ViewBuilder
    private func section(title: String, items: [HeldOfferSummary], showActions: Bool,
                         empty: String) -> some View {
        Text("\(title) (\(items.count))").appFont(.label)
        if items.isEmpty {
            Text(empty).appFont(.secondary).foregroundStyle(.secondary)
        } else {
            ForEach(items) { item in row(item, showActions: showActions) }
        }
    }

    private func row(_ item: HeldOfferSummary, showActions: Bool) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).appFont(.body).lineLimit(2)
                Text(detail(item)).appFont(.fine).foregroundStyle(.secondary)
            }
            Spacer()
            if showActions {
                Button("Show") { Task { await viewModel.open(item) } }
                    .controlSize(.small)
                    .help("Show this suggestion now")
                Button("Dismiss") { Task { await viewModel.dismiss(item) } }
                    .controlSize(.small)
            }
        }
        .padding(.vertical, 2)
    }

    private func detail(_ item: HeldOfferSummary) -> String {
        var parts: [String] = []
        if !item.source.isEmpty { parts.append(item.source.replacingOccurrences(of: "_", with: " ")) }
        if !showReason(item.reason).isEmpty { parts.append(showReason(item.reason)) }
        if let expires = item.expiresDate {
            parts.append("expires \(expires.formatted(date: .omitted, time: .shortened))")
        }
        return parts.joined(separator: " · ")
    }

    private func showReason(_ reason: String) -> String {
        switch reason {
        case "meeting": return "held: in a meeting"
        case "screen_sharing": return "held: sharing your screen"
        case "typing": return "held: you were typing"
        case "paused": return "held: paused"
        case "daily_cap": return "over today's limit"
        default: return reason
        }
    }
}

/// Settings section: the daily suggestion limit and the never-watch apps.
struct DeliverySettingsView: View {
    @ObservedObject var viewModel: DeliveryViewModel
    @State private var draftCap: Int = 8
    @State private var newEntry: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Suggestions").appFont(.headline)
            Stepper(value: $draftCap, in: 0...100) {
                Text("At most \(draftCap) suggestions a day").appFont(.body)
            }
            .onChange(of: draftCap) { _, value in
                guard var s = viewModel.settings, s.dailyCap != value else { return }
                s.dailyCap = value
                Task { await viewModel.saveSettings(s) }
            }
            Text("Suggestions over the limit wait in the Inbox. Things you ask for are never limited.")
                .appFont(.fine).foregroundStyle(.secondary)

            Text("Never watch these apps").appFont(.label)
            Text("Switching to, copying in, or reading a tab while these are in front is never reported. Bundle ids (com.example.App) or app names.")
                .appFont(.fine).foregroundStyle(.secondary)
            ForEach(viewModel.settings?.neverWatch ?? [], id: \.self) { entry in
                HStack {
                    Text(entry).appFont(.secondary, design: .monospaced)
                    Spacer()
                    Button("Remove") { remove(entry) }.controlSize(.small)
                }
            }
            HStack {
                TextField("com.example.App", text: $newEntry)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(add)
                Button("Add", action: add)
                    .disabled(newEntry.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if let error = viewModel.errorMessage {
                Text(error).appFont(.secondary).foregroundStyle(.orange)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task {
            await viewModel.loadSettings()
            if let cap = viewModel.settings?.dailyCap { draftCap = cap }
        }
    }

    private func add() {
        let entry = newEntry.trimmingCharacters(in: .whitespaces)
        guard !entry.isEmpty, var s = viewModel.settings else { return }
        if !s.neverWatch.contains(entry) { s.neverWatch.append(entry) }
        newEntry = ""
        Task { await viewModel.saveSettings(s) }
    }

    private func remove(_ entry: String) {
        guard var s = viewModel.settings else { return }
        s.neverWatch.removeAll { $0 == entry }
        Task { await viewModel.saveSettings(s) }
    }
}
