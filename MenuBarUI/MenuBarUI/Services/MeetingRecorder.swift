import Foundation
import AppKit
import AVFoundation
import CoreGraphics
import CoreMedia
import ScreenCaptureKit
import Speech
import SwiftUI

// Meeting follow-through (#86): the app records, the daemon never can.
//
// * The daemon may only OFFER (`meeting_record_offer`). Recording starts in
//   `MeetingRecorder.start(offer:consent:)` / `startManual(consent:)`, which
//   need a `MeetingRecordingConsent` that only a click handler creates.
// * While it runs, a floating indicator stays on screen with a Stop button.
//   It stops on that click, at the offer's `stop_at` (the event's end + 5
//   minutes; 2 hours for a menu-item recording), or on `meeting_record_stop`.
// * Audio goes to `~/.bot-inator/meetings/audio/` (0600). With the default
//   engine it is transcribed here with SFSpeechRecognizer and
//   `requiresOnDeviceRecognition = true` (never Apple's servers), the
//   transcript is written to `~/.bot-inator/meetings/<id>.txt` (0600), and
//   the audio is deleted. With the `whisper` engine the daemon transcribes
//   and deletes it.
// * #105: before an "apple" recording starts, `MeetingDictationCheck` makes
//   sure macOS will actually recognise speech (it refuses while Siri and
//   Dictation are both off); if not, nothing is recorded. When on-device
//   transcription fails anyway, the audio is KEPT (same folder, 0600, with a
//   `<id>.failed.json` marker) so the user can Retry or Delete it; a kept
//   recording is deleted after 24 hours (checked while running and by the
//   purge at launch).
// * Microphone lines are tagged `[me]`, system-audio lines `[others]`, so the
//   daemon knows whose "I'll ..." it heard (no speaker identification).

// MARK: - Frames

/// AVAuthorizationStatus / SFSpeechRecognizerAuthorizationStatus names.
enum MeetingAuthStatus: String, Codable, Hashable {
    case notDetermined = "not_determined"
    case restricted
    case denied
    case authorized
}

/// #99: asking for Microphone / Speech Recognition from a click in
/// Settings, not only from "Take notes". macOS lists an app under Privacy &
/// Security only after the app itself has asked, so opening the pane alone
/// left nothing to turn on.
enum MeetingPermissionKind: String, CaseIterable {
    case microphone
    case speechRecognition = "speech_recognition"

    var settingsURL: URL {
        let pane = self == .microphone ? "Privacy_Microphone" : "Privacy_SpeechRecognition"
        return URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)")!
    }
}

extension Notification.Name {
    /// Posted after a Settings click changed (or tried to change) a meeting
    /// permission, so the recorder re-reports its status to the daemon.
    static let meetingPermissionsChanged = Notification.Name("meetingPermissionsChanged")
}

@MainActor
enum MeetingPermissions {
    static func status(_ kind: MeetingPermissionKind) -> MeetingAuthStatus {
        kind == .microphone ? MeetingRecorder.microphoneStatus() : MeetingRecorder.speechStatus()
    }

    /// Not asked yet → the macOS prompt. Said no (or restricted) → open the
    /// pane, where the app is now listed. Returns the status afterwards.
    @discardableResult
    static func request(_ kind: MeetingPermissionKind) async -> MeetingAuthStatus {
        switch status(kind) {
        case .notDetermined:
            NSApp.activate(ignoringOtherApps: true)
            _ = kind == .microphone
                ? await MeetingRecorder.requestMicrophone()
                : await MeetingRecorder.requestSpeech()
        case .denied, .restricted:
            NSWorkspace.shared.open(kind.settingsURL)
        case .authorized:
            break
        }
        NotificationCenter.default.post(name: .meetingPermissionsChanged, object: nil)
        return status(kind)
    }
}

enum MeetingRecordStateKind: String, Codable, Hashable {
    case idle, started, stopped, failed, declined
}

enum MeetingTranscriptionEngine: String, Codable, Hashable {
    /// On-device SFSpeechRecognizer, in this app.
    case apple
    /// MLX Whisper, in the daemon.
    case whisper
}

/// Server→client: "Take notes for <title>?". An offer, never a start.
struct MeetingRecordOfferPayload: Codable, Hashable {
    let meetingId: String
    let title: String
    let start: String
    let end: String
    /// ISO 8601: the event's end + 5 minutes.
    let stopAt: String
    let engine: MeetingTranscriptionEngine
    let attendeeCount: Int

    enum CodingKeys: String, CodingKey {
        case meetingId = "meeting_id"
        case title, start, end
        case stopAt = "stop_at"
        case engine
        case attendeeCount = "attendee_count"
    }
}

/// Server→client: stop this recording now (the daemon's end + 5 safety net).
struct MeetingRecordStopPayload: Codable, Hashable {
    let meetingId: String
    let reason: String

    enum CodingKeys: String, CodingKey {
        case meetingId = "meeting_id"
        case reason
    }
}

/// Client→server: what the recorder is doing, plus its permissions. Sent
/// `idle` on connect, so the daemon knows this app can record.
struct MeetingRecordStatePayload: Codable, Hashable {
    var meetingId: String? = nil
    let state: MeetingRecordStateKind
    var title: String? = nil
    var stopAt: String? = nil
    let microphone: MeetingAuthStatus
    let speechRecognition: MeetingAuthStatus
    var error: String? = nil

    enum CodingKeys: String, CodingKey {
        case meetingId = "meeting_id"
        case state, title
        case stopAt = "stop_at"
        case microphone
        case speechRecognition = "speech_recognition"
        case error
    }
}

/// Client→server: a recording finished. `transcriptPath` (engine apple) or
/// `audioPath` + `systemAudioPath` (engine whisper), or `error`.
struct MeetingAudioReadyPayload: Codable, Hashable {
    let meetingId: String
    var title: String? = nil
    var startedAt: String? = nil
    var endedAt: String? = nil
    var audioPath: String? = nil
    var systemAudioPath: String? = nil
    var transcriptPath: String? = nil
    var error: String? = nil
    var stopReason: String? = nil

    enum CodingKeys: String, CodingKey {
        case meetingId = "meeting_id"
        case title
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case audioPath = "audio_path"
        case systemAudioPath = "system_audio_path"
        case transcriptPath = "transcript_path"
        case error
        case stopReason = "stop_reason"
    }
}

/// What the recorder asks the WebSocket manager to send.
enum MeetingRecorderOutgoing {
    case state(MeetingRecordStatePayload)
    case audioReady(MeetingAudioReadyPayload)
}

/// Proof that the user clicked. Create it ONLY inside the action of a
/// Button or menu item the user pressed; never from a frame handler.
struct MeetingRecordingConsent {
    fileprivate init() {}
    static func userClicked() -> MeetingRecordingConsent { MeetingRecordingConsent() }
}

struct MeetingRecorderError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

// MARK: - Files

/// #105: a recording whose on-device transcription failed. Its audio is kept
/// (0600, in the audio folder) for a Retry, at most `keepFailedFor`.
struct FailedRecording: Codable, Equatable, Identifiable {
    let meetingId: String
    let title: String
    let startedAt: String
    let endedAt: String
    var stopReason: String?
    var error: String
    let failedAt: Date
    var micPath: String?
    var systemPath: String?

    var id: String { meetingId }
    var micURL: URL? { micPath.map { URL(fileURLWithPath: $0) } }
    var systemURL: URL? { systemPath.map { URL(fileURLWithPath: $0) } }
    var expiresAt: Date { failedAt.addingTimeInterval(MeetingFiles.keepFailedFor) }
    /// macOS refused recognition because Siri and Dictation are off.
    var dictationOff: Bool { MeetingDictationCheck.isDictationOffMessage(error) }
}

enum MeetingFiles {
    static var notesDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".bot-inator/meetings", isDirectory: true)
    }
    static var audioDir: URL { notesDir.appendingPathComponent("audio", isDirectory: true) }

    static func ensureDirs() throws {
        for dir in [notesDir, audioDir] {
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
        }
    }

    static func restrict(_ url: URL) {
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    static func delete(_ url: URL?) {
        guard let url else { return }
        try? FileManager.default.removeItem(at: url)
    }

    /// #105: how long a recording whose transcription failed is kept for a
    /// Retry before it is deleted anyway.
    static let keepFailedFor: TimeInterval = 24 * 60 * 60

    static func markerURL(_ meetingId: String) -> URL {
        audioDir.appendingPathComponent("\(meetingId).failed.json")
    }

    /// Records that `failed`'s audio is kept on purpose (until it expires).
    static func saveFailed(_ failed: FailedRecording) {
        guard let data = try? JSONEncoder().encode(failed) else { return }
        let url = markerURL(failed.meetingId)
        FileManager.default.createFile(atPath: url.path, contents: data,
                                       attributes: [.posixPermissions: 0o600])
        restrict(url)
    }

    /// Deletes a kept recording: its audio files and its marker.
    static func deleteFailed(_ failed: FailedRecording) {
        delete(failed.micURL)
        delete(failed.systemURL)
        delete(markerURL(failed.meetingId))
    }

    /// Nothing records at launch, so audio left here is from a crash, an
    /// undelivered recording or a failed transcription. Deleted: everything
    /// except the audio of a failed transcription (#105) that is less than
    /// 24 hours old and still has its marker. Returns the recordings kept.
    @discardableResult
    static func purgeLeftoverAudio(now: Date = Date()) -> [FailedRecording] {
        let items = (try? FileManager.default.contentsOfDirectory(
            at: audioDir, includingPropertiesForKeys: nil)) ?? []
        var kept: [FailedRecording] = []
        var keepPaths: Set<String> = []
        for item in items where item.lastPathComponent.hasSuffix(".failed.json") {
            guard let data = try? Data(contentsOf: item),
                  let failed = try? JSONDecoder().decode(FailedRecording.self, from: data),
                  item.lastPathComponent == "\(failed.meetingId).failed.json",
                  failed.expiresAt > now else { continue }
            let audio = [failed.micURL, failed.systemURL].compactMap { $0 }
                .filter { $0.deletingLastPathComponent().standardizedFileURL.path == audioDir.standardizedFileURL.path
                    && FileManager.default.fileExists(atPath: $0.path) }
            guard !audio.isEmpty else { continue }
            kept.append(failed)
            keepPaths.insert(item.standardizedFileURL.path)
            for url in audio { keepPaths.insert(url.standardizedFileURL.path) }
        }
        for item in items where !keepPaths.contains(item.standardizedFileURL.path) { delete(item) }
        return kept.sorted { $0.failedAt > $1.failedAt }
    }

    static func writeTranscript(_ text: String, meetingId: String) throws -> URL {
        try ensureDirs()
        let url = notesDir.appendingPathComponent("\(meetingId).txt")
        guard FileManager.default.createFile(
            atPath: url.path, contents: Data(text.utf8),
            attributes: [.posixPermissions: 0o600]) else {
            throw MeetingRecorderError("Couldn't save the transcript.")
        }
        restrict(url)
        return url
    }
}

// MARK: - Recorder

@MainActor
final class MeetingRecorder: ObservableObject {
    static let shared = MeetingRecorder()

    static let noticeKey = "meetingNotes.noticeAcknowledged"
    static let systemAudioKey = "meetingNotes.systemAudio"
    static let manualMax: TimeInterval = 2 * 60 * 60

    struct Session: Equatable {
        let meetingId: String
        let title: String
        let stopAt: Date
        let engine: MeetingTranscriptionEngine
        let startedAt: Date
    }

    /// #105: "Take notes" was clicked but on-device recognition won't work,
    /// so nothing was recorded.
    struct BlockedStart: Equatable {
        let meetingId: String
        let title: String
        let stopAt: Date
        let message: String
        let dictationOff: Bool
        let whisperAvailable: Bool
    }

    enum Phase: Equatable {
        case idle
        case offered(MeetingRecordOfferPayload)
        case starting(String)
        case blocked(BlockedStart)
        case recording(Session)
        case transcribing(String)
        case failed(FailedRecording)
        case finished(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lastError: String?
    /// #105: failed transcriptions whose audio is kept for a Retry.
    @Published private(set) var failures: [FailedRecording] = []
    /// Whether the daemon has mlx-whisper (asked when a Whisper button may show).
    @Published private(set) var whisperAvailable = false
    private var expiryLoop: Task<Void, Never>?

    /// Set by the WebSocket manager: sends one frame to the daemon.
    var send: ((MeetingRecorderOutgoing) -> Void)?
    /// Whether the daemon is reachable (set by the WebSocket manager).
    var isConnected: () -> Bool = { true }

    private var mic: MicrophoneCapture?
    private var system: SystemAudioCapture?
    private var stopTask: Task<Void, Never>?
    private var expiryTask: Task<Void, Never>?
    private lazy var panel = MeetingRecorderPanelController(recorder: self)

    private var permissionsObserver: NSObjectProtocol?

    init() {
        failures = MeetingFiles.purgeLeftoverAudio()
        if let first = failures.first {
            // A failed transcription from before a restart: offer Retry again.
            phase = .failed(first)
            Task { @MainActor [weak self] in
                self?.panel.show()
                self?.watchExpiry()
            }
        }
        // #99: a Settings click asked for a permission; tell the daemon.
        permissionsObserver = NotificationCenter.default.addObserver(
            forName: .meetingPermissionsChanged, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.reportIdle() }
        }
    }

    var isRecording: Bool {
        if case .recording = phase { return true }
        return false
    }

    /// The user is told once that telling the others is their job.
    var noticeAcknowledged: Bool {
        get { UserDefaults.standard.bool(forKey: Self.noticeKey) }
        set { UserDefaults.standard.set(newValue, forKey: Self.noticeKey); objectWillChange.send() }
    }

    var includeSystemAudio: Bool {
        UserDefaults.standard.object(forKey: Self.systemAudioKey) as? Bool ?? true
    }

    // MARK: Permissions (reading never prompts)

    static func microphoneStatus() -> MeetingAuthStatus {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .authorized
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .denied
        }
    }

    static func speechStatus() -> MeetingAuthStatus {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return .authorized
        case .denied: return .denied
        case .restricted: return .restricted
        case .notDetermined: return .notDetermined
        @unknown default: return .denied
        }
    }

    /// Only after a click: may show the macOS prompt.
    static func requestMicrophone() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }

    static func requestSpeech() async -> Bool {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
        }
    }

    // MARK: Frames in

    /// The WebSocket manager calls this on connect.
    func reportIdle() {
        report(isRecording ? .started : .idle)
    }

    /// Shows the offer. Does NOT start anything.
    func receiveOffer(_ offer: MeetingRecordOfferPayload) {
        switch phase {
        case .idle, .finished, .failed, .blocked: break  // a failure stays in the menu
        default: return  // busy: one recording at a time
        }
        lastError = nil
        phase = .offered(offer)
        panel.show()
        let until = Self.parseDate(offer.stopAt) ?? Date().addingTimeInterval(Self.manualMax)
        expiryTask?.cancel()
        expiryTask = Task { [weak self] in
            let wait = until.timeIntervalSinceNow
            if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000)) }
            guard !Task.isCancelled, let self else { return }
            if case .offered(let o) = self.phase, o.meetingId == offer.meetingId {
                self.phase = .idle
                self.panel.close()
            }
        }
    }

    func receiveStop(_ payload: MeetingRecordStopPayload) {
        guard case .recording(let session) = phase, session.meetingId == payload.meetingId else { return }
        Task { await stop(reason: payload.reason) }
    }

    // MARK: User actions

    func decline() {
        guard case .offered(let offer) = phase else { return }
        expiryTask?.cancel()
        phase = .idle
        report(.declined, meetingId: offer.meetingId)
        panel.close()
    }

    func start(offer: MeetingRecordOfferPayload, consent: MeetingRecordingConsent) async {
        expiryTask?.cancel()
        let stopAt = Self.parseDate(offer.stopAt) ?? Date().addingTimeInterval(Self.manualMax)
        await begin(meetingId: offer.meetingId, title: offer.title,
                    stopAt: max(stopAt, Date().addingTimeInterval(60)), engine: offer.engine)
    }

    /// "Take notes now" from the menu: no calendar event, stops after 2 h.
    func startManual(title: String = "Meeting", consent: MeetingRecordingConsent) async {
        switch phase {
        case .idle, .finished, .offered, .failed, .blocked: break
        default: return
        }
        expiryTask?.cancel()
        let id = "manual-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
        let stopAt = Date().addingTimeInterval(Self.manualMax)
        // #105: the engine chosen in Settings (it was always "apple" here).
        let engine = await daemonEngine()
        if !noticeAcknowledged {
            // First recording ever: show the one-time notice; its button starts it.
            phase = .offered(MeetingRecordOfferPayload(
                meetingId: id, title: title, start: Self.iso(Date()), end: "",
                stopAt: Self.iso(stopAt), engine: engine, attendeeCount: 0))
            panel.show()
            return
        }
        await begin(meetingId: id, title: title, stopAt: stopAt, engine: engine)
    }

    func stopFromClick() async {
        await stop(reason: "user")
    }

    func dismissPanel() {
        switch phase {
        case .finished, .failed, .blocked: phase = .idle  // a failure stays in `failures`
        default: break
        }
        panel.close()
    }

    // MARK: #105 blocked start / failed transcription

    /// System Settings → Keyboard (Dictation is at the bottom of that pane).
    func openDictationSettings() {
        NSWorkspace.shared.open(MeetingDictationCheck.settingsURL)
    }

    /// "Try again" / "Use Whisper" on a blocked start: needs a new click.
    func retryStart(_ blocked: BlockedStart, engine: MeetingTranscriptionEngine,
                    consent: MeetingRecordingConsent) async {
        guard case .blocked(let current) = phase, current == blocked else { return }
        await begin(meetingId: blocked.meetingId, title: blocked.title,
                    stopAt: max(blocked.stopAt, Date().addingTimeInterval(60)), engine: engine)
    }

    /// The menu's "Retry meeting notes…" row: show a kept failure again.
    func showFailure(_ failed: FailedRecording) {
        switch phase {
        case .idle, .finished, .failed, .blocked, .offered: break
        default: return
        }
        guard failures.contains(where: { $0.meetingId == failed.meetingId }) else { return }
        expiryTask?.cancel()
        phase = .failed(failed)
        panel.show()
    }

    /// Re-runs on-device transcription on the kept audio.
    func retryTranscription(_ failed: FailedRecording) async {
        guard case .failed(let current) = phase, current.meetingId == failed.meetingId else { return }
        phase = .transcribing(failed.title)
        panel.show()
        await transcribeOnDevice(failed: failed)
    }

    /// Hands the kept audio to the daemon's Whisper, which deletes it after
    /// transcribing (also when that fails).
    func retryWithWhisper(_ failed: FailedRecording) {
        guard case .failed(let current) = phase, current.meetingId == failed.meetingId,
              whisperAvailable, send != nil, isConnected() else { return }
        var payload = audioReadyPayload(failed)
        payload.audioPath = failed.micPath
        payload.systemAudioPath = failed.systemPath
        MeetingFiles.delete(MeetingFiles.markerURL(failed.meetingId))
        forget(failed)
        send?(.audioReady(payload))
        phase = .finished("Notes for \(failed.title) are being transcribed with Whisper on this Mac; "
                          + "the recording is deleted when it finishes.")
        panel.show()
    }

    func deleteRecording(_ failed: FailedRecording) {
        MeetingFiles.deleteFailed(failed)
        forget(failed)
        if case .failed(let current) = phase, current.meetingId == failed.meetingId {
            phase = .finished("The recording of \(failed.title) was deleted.")
            panel.show()
        }
    }

    private func forget(_ failed: FailedRecording) {
        failures.removeAll { $0.meetingId == failed.meetingId }
    }

    /// Deletes kept recordings past 24 hours. Checks every 10 minutes while
    /// any is kept (a wall-clock check, so a sleeping Mac catches up).
    private func watchExpiry() {
        guard expiryLoop == nil, !failures.isEmpty else { return }
        expiryLoop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 600 * 1_000_000_000)
                guard let self else { return }
                let now = Date()
                for failed in self.failures where failed.expiresAt <= now {
                    MeetingFiles.deleteFailed(failed)
                    self.forget(failed)
                    if case .failed(let current) = self.phase, current.meetingId == failed.meetingId {
                        self.phase = .idle
                        self.panel.close()
                    }
                }
                if self.failures.isEmpty { self.expiryLoop = nil; return }
            }
        }
    }

    private func refreshWhisperAvailable() async {
        guard isConnected() else { whisperAvailable = false; return }
        whisperAvailable = await MeetingNotesSettingsViewModel.fetchOnce()?.whisperAvailable ?? false
    }

    /// The engine the daemon would use now; "apple" when it can't be asked.
    private func daemonEngine() async -> MeetingTranscriptionEngine {
        guard isConnected(), let settings = await MeetingNotesSettingsViewModel.fetchOnce() else { return .apple }
        whisperAvailable = settings.whisperAvailable
        return MeetingTranscriptionEngine(rawValue: settings.engineInUse) ?? .apple
    }

    // MARK: Recording

    private func begin(meetingId: String, title: String, stopAt: Date,
                       engine: MeetingTranscriptionEngine) async {
        phase = .starting(title)
        panel.show()
        guard await Self.requestMicrophone() else {
            return fail(meetingId, "Microphone access is off for bot-inator. Turn it on in System Settings → Privacy & Security → Microphone.")
        }
        if engine == .apple, !(await Self.requestSpeech()) {
            return fail(meetingId, "Speech Recognition is off for bot-inator, so notes can't be transcribed on this Mac.")
        }
        // #105: find out now, not after the meeting, whether macOS will
        // recognise speech (it refuses while Siri and Dictation are off).
        if engine == .apple {
            let readiness = await MeetingDictationCheck.check()
            if let message = readiness.blockingMessage {
                await refreshWhisperAvailable()
                lastError = message
                phase = .blocked(BlockedStart(
                    meetingId: meetingId, title: title, stopAt: stopAt, message: message,
                    dictationOff: readiness == .dictationOff, whisperAvailable: whisperAvailable))
                report(.failed, meetingId: meetingId, error: message)
                panel.show()
                return
            }
        }
        do {
            try MeetingFiles.ensureDirs()
            let micCapture = MicrophoneCapture(url: MeetingFiles.audioDir.appendingPathComponent("\(meetingId)-me.m4a"))
            try micCapture.start()
            mic = micCapture
        } catch {
            return fail(meetingId, "Couldn't start the microphone: \(error.localizedDescription)")
        }
        // The other side of a call, only when Screen Recording is already
        // granted (checked without prompting).
        if includeSystemAudio, CGPreflightScreenCaptureAccess() {
            let capture = SystemAudioCapture(url: MeetingFiles.audioDir.appendingPathComponent("\(meetingId)-others.m4a"))
            do {
                try await capture.start()
                system = capture
            } catch {
                NSLog("MeetingRecorder: system audio unavailable: \(error)")
            }
        }
        let session = Session(meetingId: meetingId, title: title, stopAt: stopAt,
                              engine: engine, startedAt: Date())
        phase = .recording(session)
        report(.started, meetingId: meetingId, title: title, stopAt: Self.iso(stopAt))
        panel.show()
        stopTask?.cancel()
        stopTask = Task { [weak self] in
            let wait = stopAt.timeIntervalSinceNow
            if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000)) }
            guard !Task.isCancelled else { return }
            await self?.stop(reason: "meeting_ended")
        }
    }

    private func stop(reason: String) async {
        guard case .recording(let session) = phase else { return }
        stopTask?.cancel()
        stopTask = nil
        let micURL = mic?.stop()
        let systemURL = await system?.stop()
        mic = nil
        system = nil
        report(.stopped, meetingId: session.meetingId)
        var payload = MeetingAudioReadyPayload(
            meetingId: session.meetingId, title: session.title,
            startedAt: Self.iso(session.startedAt), endedAt: Self.iso(Date()), stopReason: reason)

        if session.engine == .whisper, send != nil, isConnected() {
            // The daemon transcribes locally and deletes the files.
            payload.audioPath = micURL?.path
            payload.systemAudioPath = systemURL?.path
            send?(.audioReady(payload))
            phase = .finished("Notes for \(session.title) are being transcribed on this Mac.")
            panel.show()
            return
        }
        phase = .transcribing(session.title)
        panel.show()
        guard micURL != nil || systemURL != nil else {
            lastError = "Nothing was recorded."
            phase = .finished("Notes for \(session.title) failed: nothing was recorded.")
            panel.show()
            return
        }
        await transcribeOnDevice(failed: FailedRecording(
            meetingId: session.meetingId, title: session.title,
            startedAt: payload.startedAt ?? "", endedAt: payload.endedAt ?? "", stopReason: reason,
            error: "", failedAt: Date(), micPath: micURL?.path, systemPath: systemURL?.path))
    }

    private func audioReadyPayload(_ r: FailedRecording) -> MeetingAudioReadyPayload {
        MeetingAudioReadyPayload(meetingId: r.meetingId, title: r.title, startedAt: r.startedAt,
                                 endedAt: r.endedAt, stopReason: r.stopReason)
    }

    /// Transcribes `recording`'s audio here. Success: transcript saved, audio
    /// deleted, the daemon told. Failure (#105): the audio is KEPT for a
    /// Retry or Delete (24 hours at most) and the daemon hears nothing yet,
    /// so a later successful Retry still produces the one card.
    private func transcribeOnDevice(failed recording: FailedRecording) async {
        var payload = audioReadyPayload(recording)
        do {
            let text = try await MeetingTranscriber.transcribe(
                sources: [("me", recording.micURL), ("others", recording.systemURL)])
            payload.transcriptPath = try MeetingFiles.writeTranscript(text, meetingId: recording.meetingId).path
        } catch {
            // The 24 hours run from the first failure, not from each Retry.
            let earlier = failures.first { $0.meetingId == recording.meetingId }
            let failed = FailedRecording(
                meetingId: recording.meetingId, title: recording.title,
                startedAt: recording.startedAt, endedAt: recording.endedAt,
                stopReason: recording.stopReason, error: error.localizedDescription,
                failedAt: earlier?.failedAt ?? Date(),
                micPath: recording.micPath, systemPath: recording.systemPath)
            MeetingFiles.saveFailed(failed)
            forget(failed)
            failures.insert(failed, at: 0)
            lastError = failed.error
            await refreshWhisperAvailable()
            phase = .failed(failed)
            panel.show()
            watchExpiry()
            return
        }
        // Audio is deleted once the transcript exists.
        MeetingFiles.deleteFailed(recording)
        forget(recording)
        send?(.audioReady(payload))
        lastError = nil
        phase = .finished("Notes for \(recording.title) are ready; a card with the follow-ups will appear.")
        panel.show()
    }

    private func fail(_ meetingId: String, _ message: String) {
        lastError = message
        _ = mic?.stop()
        mic = nil
        phase = .finished(message)
        report(.failed, meetingId: meetingId, error: message)
        panel.show()
    }

    private func report(_ state: MeetingRecordStateKind, meetingId: String? = nil,
                        title: String? = nil, stopAt: String? = nil, error: String? = nil) {
        send?(.state(MeetingRecordStatePayload(
            meetingId: meetingId, state: state, title: title, stopAt: stopAt,
            microphone: Self.microphoneStatus(), speechRecognition: Self.speechStatus(),
            error: error)))
    }

    // MARK: Dates

    static func parseDate(_ text: String) -> Date? {
        let f = ISO8601DateFormatter()
        if let d = f.date(from: text) { return d }
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: text)
    }

    static func iso(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = .current
        return f.string(from: date)
    }
}

// MARK: - Microphone

final class MicrophoneCapture {
    let url: URL
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?

    init(url: URL) { self.url = url }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw MeetingRecorderError("No microphone input is available.")
        }
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: format.sampleRate,
            AVNumberOfChannelsKey: format.channelCount,
        ]
        let file = try AVAudioFile(forWriting: url, settings: settings,
                                   commonFormat: format.commonFormat, interleaved: format.isInterleaved)
        MeetingFiles.restrict(url)
        self.file = file
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            try? file.write(from: buffer)
        }
        engine.prepare()
        try engine.start()
    }

    /// Stops and closes the file; its URL when something was written.
    func stop() -> URL? {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        file = nil
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}

// MARK: - System audio (ScreenCaptureKit, needs Screen Recording)

final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let url: URL
    private var stream: SCStream?
    private var file: AVAudioFile?
    private let queue = DispatchQueue(label: "botinator.meeting-system-audio")

    init(url: URL) { self.url = url }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else {
            throw MeetingRecorderError("No display to capture system audio from.")
        }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48_000
        config.channelCount = 1
        // Audio only: the smallest, slowest video the API allows.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid, let pcm = Self.pcmBuffer(sampleBuffer) else { return }
        if file == nil {
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: pcm.format.sampleRate,
                AVNumberOfChannelsKey: pcm.format.channelCount,
            ]
            file = try? AVAudioFile(forWriting: url, settings: settings,
                                    commonFormat: pcm.format.commonFormat,
                                    interleaved: pcm.format.isInterleaved)
            MeetingFiles.restrict(url)
        }
        try? file?.write(from: pcm)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        NSLog("MeetingRecorder: system audio stopped: \(error)")
    }

    static func pcmBuffer(_ sampleBuffer: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let description = sampleBuffer.formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              let format = AVAudioFormat(streamDescription: asbd) else { return nil }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sampleBuffer))
        guard frames > 0, let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return nil }
        buffer.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(frames), into: buffer.mutableAudioBufferList)
        return status == noErr ? buffer : nil
    }

    func stop() async -> URL? {
        try? await stream?.stopCapture()
        stream = nil
        queue.sync { file = nil }
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}

// MARK: - Dictation check (#105)

/// Whether on-device speech recognition will actually run now.
enum SpeechReadiness: Equatable {
    case ready
    /// macOS refuses recognition: Siri and Dictation are both off.
    case dictationOff
    /// No on-device model for this language (or no recognizer at all).
    case unavailable(String)
    /// Couldn't tell (not authorized yet, or the probe gave an odd error).
    case unknown

    static let dictationOffMessage = "On-device speech recognition is refused while Dictation is off. "
        + "Turn on Dictation (System Settings → Keyboard → Dictation) or switch to Whisper."

    /// Why "Take notes" must not start, or nil when it may.
    var blockingMessage: String? {
        switch self {
        case .ready, .unknown: return nil  // unknown: a failure keeps the audio (#105)
        case .dictationOff: return Self.dictationOffMessage
        case .unavailable(let message): return message
        }
    }
}

enum MeetingDictationCheck {
    /// System Settings → Keyboard, where the Dictation switch is.
    static let settingsURL = URL(string: "x-apple.systempreferences:com.apple.preference.keyboard?Dictation")!

    /// macOS's own text for the refusal (kLSRErrorDomain 201 on macOS 14+).
    static func isDictationOffMessage(_ text: String) -> Bool {
        let t = text.lowercased()
        return t.contains("siri and dictation are disabled") || t.contains("dictation is off")
    }

    static func isDictationOffError(_ error: Error) -> Bool {
        let e = error as NSError
        return (e.domain == "kLSRErrorDomain" && e.code == 201) || isDictationOffMessage(e.localizedDescription)
    }

    /// Siri's and Dictation's switches as System Settings stores them
    /// (`com.apple.assistant.support`); nil when a key isn't there. Read
    /// without prompting (the app isn't sandboxed).
    static func preferenceSwitches() -> (dictation: Bool?, siri: Bool?) {
        let domain = "com.apple.assistant.support" as CFString
        func flag(_ key: String) -> Bool? {
            CFPreferencesAppSynchronize(domain)
            return CFPreferencesCopyAppValue(key as CFString, domain) as? Bool
        }
        return (flag("Dictation Enabled"), flag("Assistant Enabled"))
    }

    /// Never prompts. With Speech Recognition allowed, the ground truth: a
    /// tiny on-device recognition of 0.5 s of silence (what failed after the
    /// meeting fails here in about a second). Otherwise the stored switches.
    static func check() async -> SpeechReadiness {
        guard let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer() else {
            return .unavailable("Speech recognition isn't available on this Mac, so nothing can be transcribed here. Switch to Whisper.")
        }
        let switches = preferenceSwitches()
        let switchedOff = switches.dictation == false && switches.siri != true
        guard SFSpeechRecognizer.authorizationStatus() == .authorized else {
            return switchedOff ? .dictationOff : .unknown
        }
        guard recognizer.supportsOnDeviceRecognition else {
            return switchedOff ? .dictationOff : .unavailable(
                "On-device speech recognition isn't available for this language, so nothing would be transcribed. Switch to Whisper.")
        }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: 16_000, channels: 1),
              let silence = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 8_000) else {
            return switchedOff ? .dictationOff : .unknown
        }
        silence.frameLength = 8_000  // zero-filled
        do {
            _ = try await MeetingTranscriber.recognize(silence, with: recognizer, timeout: 8)
            return .ready
        } catch {
            if isDictationOffError(error) { return .dictationOff }
            NSLog("MeetingRecorder: dictation probe: \(error)")
            // "No speech detected" is the expected answer to silence.
            return switchedOff ? .dictationOff : .ready
        }
    }
}

// MARK: - On-device transcription

enum MeetingTranscriber {
    /// Each request gets one minute of audio.
    static let chunkSeconds: Double = 60

    /// Resumes a continuation exactly once (result, error or timeout).
    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var done = false
        func claim() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if done { return false }
            done = true
            return true
        }
    }

    /// `[label] text` lines in time order. Throws when nothing could be
    /// recognised at all; never uses Apple's servers.
    static func transcribe(sources: [(String, URL?)]) async throws -> String {
        guard let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(),
              recognizer.supportsOnDeviceRecognition else {
            let switches = MeetingDictationCheck.preferenceSwitches()
            if switches.dictation == false && switches.siri != true {
                throw MeetingRecorderError(SpeechReadiness.dictationOffMessage)
            }
            throw MeetingRecorderError("On-device speech recognition isn't available for this language, so nothing was transcribed (and nothing was sent anywhere).")
        }
        var pieces: [(Double, Int, String)] = []
        var lastError: Error?
        for (index, source) in sources.enumerated() {
            guard let url = source.1 else { continue }
            let file = try AVAudioFile(forReading: url)
            let format = file.processingFormat
            let chunk = AVAudioFramePosition(format.sampleRate * chunkSeconds)
            var offset: AVAudioFramePosition = 0
            while offset < file.length {
                file.framePosition = offset
                let count = AVAudioFrameCount(min(chunk, file.length - offset))
                guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: count) else { break }
                try file.read(into: buffer, frameCount: count)
                do {
                    let text = try await recognize(buffer, with: recognizer)
                    if !text.isEmpty {
                        pieces.append((Double(offset) / format.sampleRate, index, "[\(source.0)] \(text)"))
                    }
                } catch {
                    lastError = error  // e.g. "no speech detected" in a silent minute
                }
                offset += AVAudioFramePosition(count)
            }
        }
        if pieces.isEmpty, let lastError { throw lastError }
        return pieces.sorted { ($0.0, $0.1) < ($1.0, $1.1) }.map(\.2).joined(separator: "\n")
    }

    static func recognize(_ buffer: AVAudioPCMBuffer, with recognizer: SFSpeechRecognizer,
                          timeout: TimeInterval? = nil) async throws -> String {
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true  // never Apple's servers
        request.shouldReportPartialResults = false
        request.addsPunctuation = true
        request.append(buffer)
        request.endAudio()
        let once = Once()
        return try await withCheckedThrowingContinuation { cont in
            let task = recognizer.recognitionTask(with: request) { result, error in
                if let result, result.isFinal {
                    if once.claim() { cont.resume(returning: result.bestTranscription.formattedString) }
                } else if let error {
                    if once.claim() { cont.resume(throwing: error) }
                }
            }
            if let timeout {
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                    guard once.claim() else { return }
                    task.cancel()
                    cont.resume(throwing: MeetingRecorderError("Speech recognition didn't answer."))
                }
            }
        }
    }
}

// MARK: - Indicator / offer panel

struct MeetingRecorderPanelView: View {
    @ObservedObject var recorder: MeetingRecorder

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            switch recorder.phase {
            case .offered(let offer):
                Label("Take notes for \(offer.title)?", systemImage: "waveform")
                    .appFont(.headline)
                Text("Audio stays on this Mac and is deleted once it is transcribed "
                     + "(if transcription fails, it is kept up to 24 hours so you can retry). "
                     + "Afterwards you get one card with what you owe; nothing runs until you approve it.")
                    .appFont(.secondary).foregroundStyle(.secondary)
                if !recorder.noticeAcknowledged {
                    Text("Telling the other participants that you are recording is your responsibility; "
                         + "recording laws differ by place.")
                        .appFont(.secondary).foregroundStyle(.orange)
                }
                HStack {
                    Button("Not now") { recorder.decline() }
                    Spacer()
                    Button(recorder.noticeAcknowledged ? "Take notes" : "I'll tell them. Take notes") {
                        recorder.noticeAcknowledged = true
                        let consent = MeetingRecordingConsent.userClicked()
                        Task { await recorder.start(offer: offer, consent: consent) }
                    }
                    .keyboardShortcut(.defaultAction)
                }
            case .starting(let title):
                Label("Starting notes for \(title)…", systemImage: "waveform").appFont(.body)
            case .recording(let session):
                HStack(spacing: 8) {
                    Circle().fill(.red).frame(width: 10, height: 10)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Taking notes: \(session.title)").appFont(.headline)
                        Text("Stops at \(session.stopAt.formatted(date: .omitted, time: .shortened))")
                            .appFont(.fine).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Stop") { Task { await recorder.stopFromClick() } }
                }
            case .transcribing(let title):
                HStack {
                    ProgressView().controlSize(.small)
                    Text("Transcribing \(title) on this Mac…").appFont(.body)
                }
            case .blocked(let blocked):
                Label("Notes for \(blocked.title) didn't start", systemImage: "exclamationmark.triangle.fill")
                    .appFont(.headline)
                Text(blocked.message).appFont(.body)
                Text("Nothing was recorded.").appFont(.secondary).foregroundStyle(.secondary)
                if blocked.dictationOff {
                    Button("Open Dictation Settings") { recorder.openDictationSettings() }
                        .buttonStyle(.link)
                }
                HStack {
                    Button("Cancel") { recorder.dismissPanel() }
                    Spacer()
                    if blocked.whisperAvailable {
                        Button("Use Whisper") {
                            let consent = MeetingRecordingConsent.userClicked()
                            Task { await recorder.retryStart(blocked, engine: .whisper, consent: consent) }
                        }
                    }
                    Button("Try again") {
                        let consent = MeetingRecordingConsent.userClicked()
                        Task { await recorder.retryStart(blocked, engine: .apple, consent: consent) }
                    }
                    .keyboardShortcut(.defaultAction)
                }
            case .failed(let failed):
                Label("Notes for \(failed.title) failed", systemImage: "exclamationmark.triangle.fill")
                    .appFont(.headline)
                Text(failed.error).appFont(.body)
                if failed.dictationOff, !failed.error.contains("Turn on Dictation") {
                    Text("Turn on Dictation (System Settings → Keyboard → Dictation), then Retry, or use Whisper.")
                        .appFont(.secondary)
                }
                Text("The recording is kept on this Mac (only you can read it) so you can retry. "
                     + "If you don't, it is deleted automatically "
                     + "\(failed.expiresAt.formatted(date: .abbreviated, time: .shortened)) (24 hours).")
                    .appFont(.secondary).foregroundStyle(.secondary)
                HStack(spacing: 12) {
                    if failed.dictationOff {
                        Button("Open Dictation Settings") { recorder.openDictationSettings() }
                    }
                    if recorder.whisperAvailable {
                        Button("Use Whisper") { recorder.retryWithWhisper(failed) }
                    }
                }
                .buttonStyle(.link)
                HStack {
                    Button("Delete recording", role: .destructive) { recorder.deleteRecording(failed) }
                    Spacer()
                    Button("Later") { recorder.dismissPanel() }
                    Button("Retry") { Task { await recorder.retryTranscription(failed) } }
                        .keyboardShortcut(.defaultAction)
                }
            case .finished(let message):
                Text(message).appFont(.body)
                HStack { Spacer(); Button("OK") { recorder.dismissPanel() } }
            case .idle:
                EmptyView()
            }
        }
        .padding(14)
        .frame(width: 360, alignment: .leading)
    }
}

@MainActor
final class MeetingRecorderPanelController {
    private unowned let recorder: MeetingRecorder
    private var panel: NSPanel?
    private let placement = PanelPlacement()

    init(recorder: MeetingRecorder) { self.recorder = recorder }

    func show() {
        let panel = self.panel ?? makePanel()
        self.panel = panel
        // Top-right of the main screen, under the menu bar (#101: sized
        // first and kept on screen).
        if !panel.isVisible { placement.placeTopRight(panel, inset: 16, topInset: 8, screen: NSScreen.main) }
        panel.orderFrontRegardless()
    }

    func close() { panel?.orderOut(nil) }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 360, height: 120),
            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered, defer: true)
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let hosting = NSHostingController(rootView: MeetingRecorderPanelView(recorder: recorder).appTextSize())
        hosting.sizingOptions = [.preferredContentSize]
        panel.contentViewController = hosting
        return panel
    }
}

/// Menu bar menu rows: "Take notes now" / "Stop taking notes".
struct MeetingRecorderMenuItems: View {
    @ObservedObject var recorder: MeetingRecorder = .shared

    var body: some View {
        if recorder.isRecording {
            Button("Stop taking notes") { Task { await recorder.stopFromClick() } }
        } else {
            Button("Take meeting notes now") {
                let consent = MeetingRecordingConsent.userClicked()
                Task { await recorder.startManual(consent: consent) }
            }
        }
        // #105: a failed transcription whose recording is kept for a Retry.
        ForEach(recorder.failures) { failed in
            Button("Meeting notes failed: \(failed.title) (Retry or delete)…") {
                recorder.showFailure(failed)
            }
        }
    }
}
