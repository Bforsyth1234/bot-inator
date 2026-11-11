import SwiftUI

/// Response of `GET`/`PUT /api/meeting-notes` (#86).
struct MeetingNotesSettings: Codable, Equatable {
    var enabled: Bool
    var engine: String
    var engineInUse: String
    var engines: [String]
    var whisperAvailable: Bool
    var notesDir: String
    var lockedFields: [String]

    enum CodingKeys: String, CodingKey {
        case enabled, engine, engines
        case engineInUse = "engine_in_use"
        case whisperAvailable = "whisper_available"
        case notesDir = "notes_dir"
        case lockedFields = "locked_fields"
    }
}

struct MeetingNotesUpdate: Encodable {
    var enabled: Bool?
    var engine: String?
}

@MainActor
final class MeetingNotesSettingsViewModel: ObservableObject {
    @Published private(set) var settings: MeetingNotesSettings?
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

    var loaded: Bool { settings != nil }
    func isLocked(_ field: String) -> Bool { settings?.lockedFields.contains(field) ?? false }

    func refresh() async { await send(method: "GET", body: nil, status: nil) }

    func setEnabled(_ value: Bool) async {
        await send(method: "PUT", body: MeetingNotesUpdate(enabled: value, engine: nil),
                   status: value ? "Meetings will offer to take notes." : "No more note offers.")
    }

    func setEngine(_ value: String) async {
        guard value != settings?.engine else { return }
        await send(method: "PUT", body: MeetingNotesUpdate(enabled: nil, engine: value),
                   status: "Transcription engine saved.")
    }

    private func send(method: String, body: MeetingNotesUpdate?, status: String?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            var request = try DaemonAuth.request(
                baseURL.appendingPathComponent("api/meeting-notes"), method: method)
            if let body {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try JSONEncoder().encode(body)
            }
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
                throw NSError(domain: "MeetingNotes", code: http.statusCode,
                              userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP \(http.statusCode)"])
            }
            settings = try JSONDecoder().decode(MeetingNotesSettings.self, from: data)
            errorMessage = nil
            statusMessage = status
        } catch {
            errorMessage = (body == nil ? "" : "Save failed: ") + error.localizedDescription
        }
    }
}

/// "Meeting notes" section for Settings (#86). Embed inside a `Form` with
/// one line, after Meeting prep: `MeetingNotesSettingsView()`.
struct MeetingNotesSettingsView: View {
    @StateObject private var viewModel = MeetingNotesSettingsViewModel()
    @AppStorage(MeetingRecorder.systemAudioKey) private var systemAudio = true

    var body: some View {
        Section {
            Toggle("Offer to take notes in meetings", isOn: Binding(
                get: { viewModel.settings?.enabled ?? false },
                set: { value in Task { await viewModel.setEnabled(value) } }
            ))
            .disabled(!viewModel.loaded || viewModel.isLocked("enabled"))
            lockedNote("enabled", env: "AGENT_MEETING_NOTES")

            Picker("Transcribe with", selection: Binding(
                get: { viewModel.settings?.engine ?? "apple" },
                set: { value in Task { await viewModel.setEngine(value) } }
            )) {
                Text("Apple speech, on this Mac").tag("apple")
                Text("Whisper (MLX), on this Mac").tag("whisper")
            }
            .disabled(!viewModel.loaded || viewModel.isLocked("engine"))
            lockedNote("engine", env: "AGENT_MEETING_NOTES_ENGINE")
            if let s = viewModel.settings, s.engine == "whisper", !s.whisperAvailable {
                Text("mlx-whisper isn't installed in the daemon, so Apple's on-device speech is used.")
                    .appFont(.secondary).foregroundStyle(.orange)
            }

            Toggle("Include the other side of the call (system audio)", isOn: $systemAudio)
            MeetingPermissionButtons()
            Text("System audio needs Screen Recording for bot-inator; without it only your microphone is recorded.")
                .appFont(.secondary).foregroundStyle(.secondary)

            Text("Recording starts only when you click Take notes (or the menu item) and stops at the "
                 + "meeting's end + 5 minutes or when you click Stop. Audio never leaves this Mac and is "
                 + "deleted once transcribed; a cloud engine never sees the transcript. You get one card "
                 + "with reminders, a follow-up, a draft to the attendees and a note; nothing runs or is "
                 + "sent until you approve it. Telling the others you are recording is your responsibility.")
                .appFont(.secondary).foregroundStyle(.secondary)
            if let dir = viewModel.settings?.notesDir {
                Text("Transcripts are kept in \(dir).")
                    .appFont(.fine).foregroundStyle(.secondary)
            }

            if let status = viewModel.statusMessage {
                Text(status).appFont(.secondary).foregroundStyle(.secondary)
            }
            if let error = viewModel.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("Meeting notes")
        }
        .task { await viewModel.refresh() }
    }

    @ViewBuilder
    private func lockedNote(_ field: String, env: String) -> some View {
        if viewModel.isLocked(field) {
            Text("Set by \(env) in the daemon's shell; unset it there to change it here.")
                .appFont(.secondary).foregroundStyle(.secondary)
        }
    }
}

/// #99: ask for Microphone and Speech Recognition here, before the first
/// meeting, so the app shows up in System Settings to be turned on.
struct MeetingPermissionButtons: View {
    @State private var statuses: [MeetingPermissionKind: MeetingAuthStatus] = [:]

    var body: some View {
        ForEach(MeetingPermissionKind.allCases, id: \.self) { kind in
            HStack {
                Circle()
                    .fill(statuses[kind] == .authorized ? Color.green : Color.gray)
                    .frame(width: 8, height: 8)
                Text(kind == .microphone ? "Microphone" : "Speech Recognition")
                Text(label(statuses[kind])).appFont(.secondary).foregroundStyle(.secondary)
                Spacer()
                if statuses[kind] != .authorized {
                    Button(statuses[kind] == .notDetermined ? "Allow…" : "Open Settings") {
                        Task { statuses[kind] = await MeetingPermissions.request(kind) }
                    }
                }
            }
        }
        .onAppear(perform: refresh)
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            refresh()
        }
    }

    private func refresh() {
        for kind in MeetingPermissionKind.allCases { statuses[kind] = MeetingPermissions.status(kind) }
    }

    private func label(_ status: MeetingAuthStatus?) -> String {
        switch status {
        case .authorized: return "allowed"
        case .notDetermined: return "not asked yet"
        case .denied: return "turned off"
        case .restricted: return "restricted"
        case nil: return ""
        }
    }
}
