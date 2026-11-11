import SwiftUI

/// "Watch for…" (#88): the user writes a trigger in one sentence, the daemon
/// turns it into a restricted spec (one model call), shows it in plain words
/// with a 30-day backtest, and the user saves it. A watch only ever raises a
/// card to approve; it never acts on its own.
///
/// Routes: `POST /api/watches/preview`, `GET/POST /api/watches`,
/// `PATCH/DELETE /api/watches/{id}`.

/// A free-form JSON value, so the spec round-trips to the daemon unchanged.
indirect enum WatchJSON: Codable, Hashable {
    case string(String), number(Double), bool(Bool), object([String: WatchJSON]), array([WatchJSON]), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([WatchJSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: WatchJSON].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .object(let o): try c.encode(o)
        case .array(let a): try c.encode(a)
        case .null: try c.encodeNil()
        }
    }
}

struct WatchExample: Codable, Hashable {
    let when: String
    let text: String
}

struct WatchSourceCheck: Codable, Hashable {
    let name: String
    let status: String
    let count: Int?
    let reason: String
}

struct WatchBacktest: Codable, Hashable {
    let days: Int
    let matches: Int?
    let examples: [WatchExample]
    let sources: [WatchSourceCheck]
    let summary: String
}

struct WatchPreview: Codable {
    let spec: [String: WatchJSON]?
    let words: String?
    let backtest: WatchBacktest?
    let error: String?
    let problems: [String]?
}

struct Watch: Codable, Hashable, Identifiable {
    let id: String
    let sentence: String
    let words: String
    let enabled: Bool
    let dailyLimit: Int
    let cooldownMinutes: Int
    let pausedReason: String?
    let firedToday: Int

    enum CodingKeys: String, CodingKey {
        case id, sentence, words, enabled
        case dailyLimit = "daily_limit"
        case cooldownMinutes = "cooldown_minutes"
        case pausedReason = "paused_reason"
        case firedToday = "fired_today"
    }
}

private struct WatchSaveBody: Encodable {
    let spec: [String: WatchJSON]
    let sentence: String
    let daily_limit: Int
    let cooldown_minutes: Int
}

@MainActor
final class WatchesViewModel: ObservableObject {
    @Published var sentence: String = ""
    @Published var dailyLimit: Int = 5
    @Published var cooldownMinutes: Int = 30
    @Published private(set) var preview: WatchPreview?
    @Published private(set) var watches: [Watch] = []
    @Published private(set) var isWorking: Bool = false
    @Published var errorMessage: String?

    static let maxWatches = 20

    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    private func url(_ path: String) -> URL {
        baseURL.appendingPathComponent(path)
    }

    private func send(_ path: String, method: String, body: Data? = nil) async throws -> Data {
        var request = try DaemonAuth.request(url(path), method: method)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        try DaemonAuth.checkAuth(response)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
            throw NSError(domain: "Watches", code: http.statusCode, userInfo: [
                NSLocalizedDescriptionKey: (detail as? String) ?? "HTTP \(http.statusCode)",
            ])
        }
        return data
    }

    func refresh() async {
        do {
            let data = try await send("api/watches", method: "GET")
            watches = try JSONDecoder().decode([Watch].self, from: data)
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load watches: \(error.localizedDescription)"
        }
    }

    func check() async {
        let text = sentence.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        isWorking = true
        defer { isWorking = false }
        do {
            let body = try JSONEncoder().encode(["sentence": text])
            let data = try await send("api/watches/preview", method: "POST", body: body)
            preview = try JSONDecoder().decode(WatchPreview.self, from: data)
            errorMessage = nil
        } catch {
            preview = nil
            errorMessage = "Failed to check the watch: \(error.localizedDescription)"
        }
    }

    func save() async {
        guard let spec = preview?.spec else { return }
        isWorking = true
        defer { isWorking = false }
        do {
            let body = try JSONEncoder().encode(WatchSaveBody(
                spec: spec, sentence: sentence, daily_limit: dailyLimit, cooldown_minutes: cooldownMinutes))
            _ = try await send("api/watches", method: "POST", body: body)
            preview = nil
            sentence = ""
            errorMessage = nil
            await refresh()
        } catch {
            errorMessage = "Failed to save the watch: \(error.localizedDescription)"
        }
    }

    func discard() {
        preview = nil
    }

    func setEnabled(_ watch: Watch, _ enabled: Bool) async {
        do {
            let body = try JSONEncoder().encode(["enabled": enabled])
            _ = try await send("api/watches/\(watch.id)", method: "PATCH", body: body)
            await refresh()
        } catch {
            errorMessage = "Failed to update the watch: \(error.localizedDescription)"
        }
    }

    func remove(_ watch: Watch) async {
        do {
            _ = try await send("api/watches/\(watch.id)", method: "DELETE")
            await refresh()
        } catch {
            errorMessage = "Failed to delete the watch: \(error.localizedDescription)"
        }
    }
}

/// Self-contained "Watch for…" section: embed with `WatchesView()`.
struct WatchesView: View {
    @StateObject private var viewModel = WatchesViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            header
            Text("Describe something to watch for in one sentence, e.g. “when a PDF lands in Downloads, offer to move it to Documents/Receipts”. A watch only offers: every time, you get a card to approve.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            composer
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.secondary)
                    .foregroundStyle(.orange)
            }
            if let preview = viewModel.preview {
                previewBox(preview)
            }
            savedList
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .task { await viewModel.refresh() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "binoculars")
                .foregroundStyle(.secondary)
            Text("Watch for…").appFont(.headline)
            Text("\(viewModel.watches.count)/\(WatchesViewModel.maxWatches)")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                Task { await viewModel.refresh() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Reload watches")
        }
    }

    private var composer: some View {
        HStack(spacing: 8) {
            TextField("When … offer to …", text: $viewModel.sentence)
                .textFieldStyle(.roundedBorder)
                .appFont(.body)
                .onSubmit { Task { await viewModel.check() } }
            Button("Check") {
                Task { await viewModel.check() }
            }
            .disabled(viewModel.isWorking
                      || viewModel.sentence.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                      || viewModel.watches.count >= WatchesViewModel.maxWatches)
            if viewModel.isWorking {
                ProgressView().controlSize(.small)
            }
        }
    }

    @ViewBuilder
    private func previewBox(_ preview: WatchPreview) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = preview.error {
                Label(error, systemImage: "xmark.octagon")
                    .appFont(.callout)
                    .foregroundStyle(.red)
                ForEach(preview.problems ?? [], id: \.self) { problem in
                    Text("• \(problem)").appFont(.fine).foregroundStyle(.secondary)
                }
            }
            if let words = preview.words {
                Text(words).appFont(.body).fixedSize(horizontal: false, vertical: true)
            }
            if let test = preview.backtest {
                Text(test.summary).appFont(.label)
                ForEach(Array(test.examples.enumerated()), id: \.offset) { _, example in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(example.when).appFont(.fine).foregroundStyle(.secondary)
                        Text(example.text).appFont(.secondary).lineLimit(1)
                    }
                }
                ForEach(test.sources.filter { $0.status != "ok" }, id: \.self) { src in
                    Text("\(src.name): \(src.reason)")
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
            }
            if preview.spec != nil {
                HStack(spacing: 10) {
                    Stepper("Up to \(viewModel.dailyLimit) cards a day",
                            value: $viewModel.dailyLimit, in: 1...20)
                        .appFont(.secondary)
                    Stepper("Wait \(viewModel.cooldownMinutes) min between cards",
                            value: $viewModel.cooldownMinutes, in: 0...1440, step: 5)
                        .appFont(.secondary)
                }
                HStack {
                    Button("Save watch") { Task { await viewModel.save() } }
                        .keyboardShortcut(.defaultAction)
                        .disabled(viewModel.isWorking)
                    Button("Discard") { viewModel.discard() }
                }
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
    }

    @ViewBuilder
    private var savedList: some View {
        if viewModel.watches.isEmpty {
            Text("No watches yet.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else {
            List {
                ForEach(viewModel.watches) { watch in
                    row(for: watch)
                }
            }
            .listStyle(.inset)
            .frame(minHeight: 120, maxHeight: 240)
        }
    }

    private func row(for watch: Watch) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Toggle("", isOn: Binding(
                get: { watch.enabled },
                set: { value in Task { await viewModel.setEnabled(watch, value) } }))
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(.small)
            VStack(alignment: .leading, spacing: 2) {
                Text(watch.words).appFont(.body).lineLimit(3)
                Text(detail(for: watch))
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                if let reason = watch.pausedReason, !reason.isEmpty, !watch.enabled {
                    Text(reason).appFont(.fine).foregroundStyle(.orange)
                }
            }
            Spacer()
            Button("Delete", role: .destructive) {
                Task { await viewModel.remove(watch) }
            }
            .controlSize(.small)
        }
        .padding(.vertical, 2)
    }

    private func detail(for watch: Watch) -> String {
        var parts = ["\(watch.firedToday)/\(watch.dailyLimit) cards today",
                     "\(watch.cooldownMinutes) min cooldown"]
        if !watch.sentence.isEmpty { parts.insert("“\(watch.sentence)”", at: 0) }
        return parts.joined(separator: " · ")
    }
}
