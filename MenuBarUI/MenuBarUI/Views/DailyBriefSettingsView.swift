import SwiftUI

/// Response of `GET`/`PUT /api/daily-brief` (#65).
private struct DailyBriefSettings: Codable, Equatable {
    var enabled: Bool
    var time: String            // "HH:MM", 24-hour, local
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, time
        case lockedFields = "locked_fields"
    }
}

private struct DailyBriefUpdate: Encodable {
    var enabled: Bool?
    var time: String?
}

private struct DailyBriefRunResponse: Codable {
    let status: String
    let unavailable: [String]?
}

@MainActor
final class DailyBriefSettingsViewModel: ObservableObject {
    @Published private(set) var enabled = true
    @Published private(set) var time = DailyBriefSettingsViewModel.date(from: "08:00")
    @Published private(set) var lockedFields: [String] = []
    @Published private(set) var loaded = false
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

    func isLocked(_ field: String) -> Bool { lockedFields.contains(field) }

    func refresh() async {
        await send(method: "GET", body: nil, status: nil)
    }

    /// Saved as soon as it is flipped: the daemon reads it on every check.
    func setEnabled(_ value: Bool) async {
        enabled = value
        await send(method: "PUT", body: DailyBriefUpdate(enabled: value, time: nil),
                   status: value ? "Morning brief is on." : "Morning brief is off.")
    }

    func setTime(_ value: Date) async {
        let text = Self.text(from: value)
        guard text != Self.text(from: time) else { return }
        time = value
        await send(method: "PUT", body: DailyBriefUpdate(enabled: nil, time: text),
                   status: "Morning brief time saved.")
    }

    /// "Brief me now": the daemon publishes one brief; it shows in Thoughts.
    func briefNow() async {
        isBusy = true
        defer { isBusy = false }
        do {
            let request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/daily-brief/run"), method: "POST"
            )
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            let reply = try JSONDecoder().decode(DailyBriefRunResponse.self, from: data)
            errorMessage = nil
            if let missing = reply.unavailable, !missing.isEmpty {
                statusMessage = "Brief requested. Unavailable: \(missing.joined(separator: ", ")). See the Thoughts tab."
            } else {
                statusMessage = "Brief requested. See the Thoughts tab."
            }
        } catch {
            errorMessage = "Brief failed: \(error.localizedDescription)"
        }
    }

    private func send(method: String, body: DailyBriefUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/daily-brief"), method: method
            )
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            try Self.checkStatus(response, data)
            apply(try JSONDecoder().decode(DailyBriefSettings.self, from: data))
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (body == nil ? "" : "Save failed: ") + error.localizedDescription
            if body != nil { await reload() }
        }
    }

    /// After a failed save, show what the daemon actually has.
    private func reload() async {
        guard let request = try? DaemonAuth.request(
            baseURL.appendingPathComponent("api/daily-brief")
        ), let (data, _) = try? await session.data(for: request),
        let settings = try? JSONDecoder().decode(DailyBriefSettings.self, from: data)
        else { return }
        apply(settings)
    }

    private func apply(_ settings: DailyBriefSettings) {
        enabled = settings.enabled
        time = Self.date(from: settings.time)
        lockedFields = settings.lockedFields
        loaded = true
    }

    static func date(from text: String) -> Date {
        let parts = text.split(separator: ":").compactMap { Int($0) }
        var components = DateComponents()
        components.hour = parts.count == 2 ? parts[0] : 8
        components.minute = parts.count == 2 ? parts[1] : 0
        return Calendar.current.date(from: components) ?? Date()
    }

    static func text(from date: Date) -> String {
        let components = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", components.hour ?? 8, components.minute ?? 0)
    }

    private static func checkStatus(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse,
              !(200..<300).contains(http.statusCode) else { return }
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
        throw NSError(
            domain: "DailyBrief", code: http.statusCode,
            userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"]
        )
    }
}

/// "Morning brief" section for Settings (#65). Embed inside a `Form` with
/// one line: `DailyBriefSettingsView()`.
struct DailyBriefSettingsView: View {
    @StateObject private var viewModel = DailyBriefSettingsViewModel()

    var body: some View {
        Section {
            Toggle("Send a morning brief", isOn: Binding(
                get: { viewModel.enabled },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_DAILY_BRIEF")

            DatePicker("Time", selection: Binding(
                get: { viewModel.time },
                set: { value in Task { await viewModel.setTime(value) } }
            ), displayedComponents: .hourAndMinute)
            .disabled(!viewModel.loaded || !viewModel.enabled || viewModel.isLocked("time"))
            lockedNote("time", env: "AGENT_DAILY_BRIEF_TIME")

            Text("Once a day: today's meetings, mail from the last 24 hours and reminders due. "
                 + "If the Mac was asleep at that time, it arrives when it wakes. It only reads; nothing is sent or changed.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)

            HStack {
                Button("Brief me now") { Task { await viewModel.briefNow() } }
                    .disabled(!viewModel.loaded || viewModel.isBusy)
                if viewModel.isBusy { ProgressView().controlSize(.small) }
                Spacer()
            }
            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Morning brief")
        }
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func lockedNote(_ field: String, env: String) -> some View {
        if viewModel.isLocked(field) {
            Text("Set by \(env) in the daemon's shell; unset it there to change it here.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        }
    }
}
