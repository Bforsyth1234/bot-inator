import SwiftUI

/// Response of `/api/screen-watch` (#147).
private struct ScreenWatchState: Codable, Equatable {
    struct RouteInfo: Codable, Equatable {
        var ok: Bool
        var kind: String
        var describe: String
        var refused: String
    }

    var enabled: Bool
    var active: Bool
    var secondsLeft: Int
    var captures: Int
    var skipped: Int
    var lastReason: String
    var lockedFields: [String]
    var route: RouteInfo

    enum CodingKeys: String, CodingKey {
        case enabled, active, captures, skipped, route
        case secondsLeft = "seconds_left"
        case lastReason = "last_reason"
        case lockedFields = "locked_fields"
    }
}

/// Shared by Settings and the menu bar item, so both show the same session.
@MainActor
final class ScreenWatchModel: ObservableObject {
    static let shared = ScreenWatchModel()

    /// Session lengths the daemon accepts (minutes), with menu labels.
    static let lengths: [(minutes: Int, label: String)] = [
        (15, "15 minutes"), (60, "1 hour"), (240, "4 hours"),
    ]

    @Published private(set) var enabled = false
    @Published private(set) var active = false
    @Published private(set) var secondsLeft = 0
    @Published private(set) var captures = 0
    @Published private(set) var skipped = 0
    @Published private(set) var lastReason = ""
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var routeText = ""
    @Published private(set) var routeIsCloud = false
    @Published private(set) var loaded = false
    @Published var errorMessage: String?

    private let baseURL = URL(string: "http://127.0.0.1:8000")!
    private var ticker: Timer?

    private init() {
        // Poll slowly so the menu's "N min left" and the icon stay current.
        ticker = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
        Task { await refresh() }
    }

    var minutesLeft: Int { max(1, (secondsLeft + 59) / 60) }

    func isLocked(_ field: String) -> Bool { lockedFields.contains(field) }

    func refresh() async { await send("GET", path: "", body: nil) }

    func setEnabled(_ value: Bool) async {
        enabled = value
        await send("PUT", path: "", body: ["enabled": value])
    }

    func start(minutes: Int) async {
        await send("POST", path: "/start", body: ["minutes": minutes])
    }

    func stop() async { await send("POST", path: "/stop", body: nil) }

    private func send(_ method: String, path: String, body: [String: Any]?) async {
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/screen-watch" + path), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
            }
            let (data, response) = try await URLSession.shared.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
                    as? String ?? "HTTP \(http.statusCode)"
                throw NSError(domain: "ScreenWatch", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: detail])
            }
            let state = try JSONDecoder().decode(ScreenWatchState.self, from: data)
            enabled = state.enabled
            active = state.active
            secondsLeft = state.secondsLeft
            captures = state.captures
            skipped = state.skipped
            lastReason = state.lastReason
            lockedFields = state.lockedFields
            routeText = state.route.ok ? state.route.describe : state.route.refused
            routeIsCloud = state.route.ok && state.route.kind == "cloud"
            loaded = true
            errorMessage = nil
        } catch {
            if method != "GET" { errorMessage = error.localizedDescription }
        }
    }
}

/// "Screen watching" section for Settings (#147).
struct ScreenWatchSettingsView: View {
    @ObservedObject private var model = ScreenWatchModel.shared

    var body: some View {
        Section {
            Toggle("Allow screen watching sessions", isOn: Binding(
                get: { model.enabled },
                set: { value in Task { await model.setEnabled(value) } }
            ))
            .disabled(!model.loaded || model.isLocked("enabled"))
            if model.isLocked("enabled") {
                Text("Set by AGENT_SCREEN_WATCH in the daemon's shell; unset it there to change it here.")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            Text("While a session runs, bot-inator captures the window in front when you switch "
                 + "windows and every 2 minutes while you're active, asks the vision model to "
                 + "describe it, and deletes the image. Descriptions only feed suggestions; nothing "
                 + "runs without a card. Password fields, never-watch apps, private windows and "
                 + "sensitive sites are skipped.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            if !model.routeText.isEmpty {
                Text("Vision model: \(model.routeText)"
                     + (model.routeIsCloud ? " (screenshots leave this Mac)" : ""))
                    .appFont(.secondary)
                    .foregroundStyle(model.routeIsCloud ? .orange : .secondary)
            }
            if model.active {
                HStack {
                    Label("Watching: \(model.minutesLeft) min left · \(model.captures) captured",
                          systemImage: "eye")
                    Spacer()
                    Button("Stop") { Task { await model.stop() } }
                }
            } else {
                HStack {
                    Text("Start a session:")
                    ForEach(ScreenWatchModel.lengths, id: \.minutes) { length in
                        Button(length.label) { Task { await model.start(minutes: length.minutes) } }
                    }
                }
                .disabled(!model.enabled)
            }
            if !model.lastReason.isEmpty {
                Text("Last skipped: \(model.lastReason)")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
            if let error = model.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Screen watching")
        }
        .task { await model.refresh() }
    }
}
