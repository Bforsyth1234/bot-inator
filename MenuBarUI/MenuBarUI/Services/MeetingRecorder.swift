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
//   the audio is deleted, also on every failure. With the `whisper` engine
//   the daemon transcribes and deletes it.
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

    /// Nothing records at launch, so any audio left here is from a crash or
    /// an undelivered recording: delete it (audio never outlives its use).
    static func purgeLeftoverAudio() {
        let items = (try? FileManager.default.contentsOfDirectory(
            at: audioDir, includingPropertiesForKeys: nil)) ?? []
        for item in items { delete(item) }
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

    enum Phase: Equatable {
        case idle
        case offered(MeetingRecordOfferPayload)
        case starting(String)
        case recording(Session)
        case transcribing(String)
        case finished(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var lastError: String?

    /// Set by the WebSocket manager: sends one frame to the daemon.
    var send: ((MeetingRecorderOutgoing) -> Void)?
    /// Whether the daemon is reachable (set by the WebSocket manager).
    var isConnected: () -> Bool = { true }

    private var mic: MicrophoneCapture?
    private var system: SystemAudioCapture?
    private var stopTask: Task<Void, Never>?
    private var expiryTask: Task<Void, Never>?
    private lazy var panel = MeetingRecorderPanelController(recorder: self)

    init() {
        MeetingFiles.purgeLeftoverAudio()
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
    private static func requestMicrophone() async -> Bool {
        await AVCaptureDevice.requestAccess(for: .audio)
    }

    private static func requestSpeech() async -> Bool {
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
        case .idle, .finished: break
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
        case .idle, .finished, .offered: break
        default: return
        }
        expiryTask?.cancel()
        let id = "manual-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
        let stopAt = Date().addingTimeInterval(Self.manualMax)
        if !noticeAcknowledged {
            // First recording ever: show the one-time notice; its button starts it.
            phase = .offered(MeetingRecordOfferPayload(
                meetingId: id, title: title, start: Self.iso(Date()), end: "",
                stopAt: Self.iso(stopAt), engine: .apple, attendeeCount: 0))
            panel.show()
            return
        }
        await begin(meetingId: id, title: title, stopAt: stopAt, engine: .apple)
    }

    func stopFromClick() async {
        await stop(reason: "user")
    }

    func dismissPanel() {
        if case .finished = phase { phase = .idle }
        panel.close()
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
        do {
            let text = try await MeetingTranscriber.transcribe(sources: [("me", micURL), ("others", systemURL)])
            payload.transcriptPath = try MeetingFiles.writeTranscript(text, meetingId: session.meetingId).path
        } catch {
            payload.error = error.localizedDescription
        }
        // Audio is deleted once the transcript exists, and on failure.
        MeetingFiles.delete(micURL)
        MeetingFiles.delete(systemURL)
        send?(.audioReady(payload))
        if let error = payload.error {
            lastError = error
            phase = .finished("Notes for \(session.title) failed: \(error) The recording was deleted.")
        } else {
            phase = .finished("Notes for \(session.title) are ready; a card with the follow-ups will appear.")
        }
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

// MARK: - On-device transcription

enum MeetingTranscriber {
    /// Each request gets one minute of audio.
    static let chunkSeconds: Double = 60

    private final class Once: @unchecked Sendable {
        var done = false
    }

    /// `[label] text` lines in time order. Throws when nothing could be
    /// recognised at all; never uses Apple's servers.
    static func transcribe(sources: [(String, URL?)]) async throws -> String {
        guard let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(),
              recognizer.supportsOnDeviceRecognition else {
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

    private static func recognize(_ buffer: AVAudioPCMBuffer,
                                  with recognizer: SFSpeechRecognizer) async throws -> String {
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true  // never Apple's servers
        request.shouldReportPartialResults = false
        request.addsPunctuation = true
        request.append(buffer)
        request.endAudio()
        let once = Once()
        return try await withCheckedThrowingContinuation { cont in
            recognizer.recognitionTask(with: request) { result, error in
                guard !once.done else { return }
                if let result, result.isFinal {
                    once.done = true
                    cont.resume(returning: result.bestTranscription.formattedString)
                } else if let error {
                    once.done = true
                    cont.resume(throwing: error)
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
                Text("Audio stays on this Mac and is deleted once it is transcribed. "
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

    init(recorder: MeetingRecorder) { self.recorder = recorder }

    func show() {
        let panel = self.panel ?? makePanel()
        self.panel = panel
        if !panel.isVisible { position(panel) }
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

    /// Top-right of the main screen, under the menu bar.
    private func position(_ panel: NSPanel) {
        guard let frame = NSScreen.main?.visibleFrame else { panel.center(); return }
        panel.setFrameTopLeftPoint(NSPoint(x: frame.maxX - panel.frame.width - 16, y: frame.maxY - 8))
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
    }
}
