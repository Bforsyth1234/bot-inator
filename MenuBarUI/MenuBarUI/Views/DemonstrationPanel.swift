import AppKit
import Combine
import SwiftUI

// MARK: - Protocol payloads (#84)

/// One reviewed line of a demonstration (`demonstration_trace.steps`).
struct DemonstrationStep: Codable, Hashable, Identifiable {
    let id: String
    let kind: String
    let text: String
}

enum DemonstrationState: String, Codable, Hashable {
    case idle, recording, stopped, drafted, refused
}

/// Server→client `demonstration_trace`: the recording's state and its
/// summarized steps. Sent on start, on every recorded step, on stop and
/// after a draft. The trace lives in the daemon's memory only.
struct DemonstrationTracePayload: Codable, Hashable {
    let recordingId: String?
    let state: DemonstrationState
    /// Epoch seconds.
    let startedAt: Double?
    /// The 5-minute hard stop (epoch seconds); nil unless recording.
    let endsAt: Double?
    let stopReason: String?
    let steps: [DemonstrationStep]
    /// Why a recording or a draft was refused (e.g. a cloud engine).
    let reason: String?
    /// The tool name the published idea proposes (state `drafted`).
    let toolName: String?
    let deferredCount: Int?

    enum CodingKeys: String, CodingKey {
        case recordingId = "recording_id"
        case state
        case startedAt = "started_at"
        case endsAt = "ends_at"
        case stopReason = "stop_reason"
        case steps, reason
        case toolName = "tool_name"
        case deferredCount = "deferred_count"
    }

    static let idle = DemonstrationTracePayload(
        recordingId: nil, state: .idle, startedAt: nil, endsAt: nil, stopReason: nil,
        steps: [], reason: nil, toolName: nil, deferredCount: nil)
}

/// Client→server `demonstration_draft`: the sentence and the lines the
/// user removed. The daemon builds the idea from the remaining lines.
struct DemonstrationDraftPayload: Codable, Hashable {
    let sentence: String
    let removedStepIds: [String]

    enum CodingKeys: String, CodingKey {
        case sentence
        case removedStepIds = "removed_step_ids"
    }
}

/// What the panel asks the daemon to do. The app wires these to
/// `WebSocketManager` (`demonstration_start` / `_stop` / `_cancel` commands
/// and the `demonstration_draft` message).
struct DemonstrationActions {
    var start: @MainActor () -> Void
    var stop: @MainActor () -> Void
    var cancel: @MainActor () -> Void
    var draft: @MainActor (DemonstrationDraftPayload) -> Void
}

// MARK: - Model

@MainActor
final class DemonstrationModel: ObservableObject {
    @Published private(set) var trace = DemonstrationTracePayload.idle
    @Published private(set) var removed: Set<String> = []
    @Published var sentence = ""
    @Published private(set) var now = Date()
    /// True between "Make a tool" and the daemon's answer.
    @Published private(set) var isSending = false

    private let actions: DemonstrationActions
    private var scanned = 0
    private var messagesCancellable: AnyCancellable?
    private var tick: AnyCancellable?

    init(actions: DemonstrationActions) {
        self.actions = actions
    }

    /// Follow a message stream (e.g. `ws.$messages`), taking each new
    /// `demonstration_trace` that `extract` finds.
    func attach<M>(messages: AnyPublisher<[M], Never>,
                   extract: @escaping (M) -> DemonstrationTracePayload?) {
        messagesCancellable = messages
            .receive(on: RunLoop.main)
            .sink { [weak self] list in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    if list.count < self.scanned { self.scanned = 0 }
                    for msg in list[self.scanned...] {
                        if let payload = extract(msg) { self.ingest(payload) }
                    }
                    self.scanned = list.count
                }
            }
    }

    var isRecording: Bool { trace.state == .recording }

    var visibleSteps: [DemonstrationStep] {
        trace.steps.filter { !removed.contains($0.id) }
    }

    var canDraft: Bool {
        trace.state == .stopped && !visibleSteps.isEmpty && !isSending
            && !sentence.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Seconds left before the hard stop, while recording.
    var secondsLeft: Int? {
        guard isRecording, let ends = trace.endsAt else { return nil }
        return max(0, Int(ends - now.timeIntervalSince1970))
    }

    func ingest(_ payload: DemonstrationTracePayload) {
        let newRecording = payload.recordingId != trace.recordingId
        trace = payload
        isSending = false
        if newRecording || payload.state == .drafted || payload.state == .idle { removed = [] }
        if newRecording { sentence = "" }
        updateTick()
    }

    func remove(_ step: DemonstrationStep) { removed.insert(step.id) }
    func restoreRemoved() { removed = [] }

    /// The hotkey and the menu item: start when idle, stop when recording.
    func toggleRecording() {
        if isRecording { actions.stop() } else { actions.start() }
    }

    func start() { actions.start() }
    func stop() { actions.stop() }

    func cancel() {
        removed = []
        sentence = ""
        actions.cancel()
    }

    func draft() {
        guard canDraft else { return }
        isSending = true
        let ids = trace.steps.map(\.id).filter { removed.contains($0) }
        actions.draft(DemonstrationDraftPayload(
            sentence: sentence.trimmingCharacters(in: .whitespacesAndNewlines),
            removedStepIds: ids))
    }

    private func updateTick() {
        if isRecording {
            guard tick == nil else { return }
            tick = Timer.publish(every: 1, on: .main, in: .common)
                .autoconnect()
                .sink { [weak self] date in
                    MainActor.assumeIsolated { self?.now = date }
                }
        } else {
            tick = nil
        }
    }
}

// MARK: - View

struct DemonstrationView: View {
    @ObservedObject var model: DemonstrationModel
    var close: () -> Void
    @Environment(\.textSize) private var textSize

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            switch model.trace.state {
            case .idle: idleBody
            case .recording: stepList(editable: false)
            case .stopped: reviewBody
            case .drafted: draftedBody
            case .refused: refusedBody
            }
        }
        .padding(14)
        .frame(width: 460 * textSize.layoutScale, alignment: .leading)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: model.isRecording ? "record.circle.fill" : "record.circle")
                .appFont(.title3)
                .foregroundStyle(model.isRecording ? Color.red : Color.secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text("Watch me").appFont(.headline)
                Text(subtitle).appFont(.secondary).foregroundStyle(.secondary)
            }
            Spacer()
            if model.isRecording {
                Button("Stop") { model.stop() }
                    .keyboardShortcut(.defaultAction)
            }
            Button(action: close) {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Close (Esc). A recording keeps going.")
        }
    }

    private var subtitle: String {
        switch model.trace.state {
        case .idle: return "Do a task once; get a tool that does it."
        case .recording:
            let left = model.secondsLeft.map { " · stops in \($0 / 60):" + String(format: "%02d", $0 % 60) } ?? ""
            return "Recording · \(model.trace.steps.count) steps\(left)"
        case .stopped:
            return model.trace.stopReason == "time"
                ? "Stopped after 5 minutes. Review the steps."
                : "Review the steps, then say what the task was."
        case .drafted: return "Sent as a tool idea."
        case .refused: return "Can't record right now."
        }
    }

    private var idleBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recording keeps file names, folders, app names, window titles and the browser tab's address, and whether you copied something (never what). It stays on this Mac, you see every line before anything is drafted, and it's dropped afterwards. Stops by itself after 5 minutes.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button("Start recording") { model.start() }
                    .keyboardShortcut(.defaultAction)
                Text(shortcutHint).appFont(.fine).foregroundStyle(.secondary)
            }
        }
    }

    private var shortcutHint: String {
        let s = RecordShortcut.current
        return s == .off ? "" : "or press \(s.label) to start and stop"
    }

    @ViewBuilder
    private func stepList(editable: Bool) -> some View {
        let steps = editable ? model.visibleSteps : model.trace.steps
        if steps.isEmpty {
            Text(model.isRecording ? "Nothing yet. Go ahead and do the task." : "No steps left.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(steps.enumerated()), id: \.element.id) { index, step in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("\(index + 1).")
                                .appFont(.secondary)
                                .foregroundStyle(.secondary)
                                .monospacedDigit()
                            Text(step.text)
                                .appFont(.body)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 4)
                            if editable {
                                Button { model.remove(step) } label: {
                                    Image(systemName: "minus.circle")
                                }
                                .buttonStyle(.borderless)
                                .help("Leave this line out of the tool")
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 260 * textSize.layoutScale)
        }
    }

    private var reviewBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            stepList(editable: true)
            if !model.removed.isEmpty {
                Button("Put back \(model.removed.count) removed") { model.restoreRemoved() }
                    .buttonStyle(.link)
                    .appFont(.secondary)
            }
            TextField("In a sentence, what was the task?", text: $model.sentence, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...3)
                .onSubmit { model.draft() }
            if let reason = model.trace.reason {
                Text(reason).appFont(.fine).foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Discard") { model.cancel() }
                Spacer()
                if model.isSending { ProgressView().controlSize(.small) }
                Button("Make a tool from this") { model.draft() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.canDraft)
            }
            Text("You'll see the idea, then the code, before anything is installed.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
        }
    }

    private var draftedBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.trace.toolName.map { "Proposed \($0). An approval card follows." }
                 ?? "An approval card follows.")
                .appFont(.body)
            Text("The recording was dropped.").appFont(.fine).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Done", action: close).keyboardShortcut(.defaultAction)
            }
        }
    }

    private var refusedBody: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.trace.reason ?? "The daemon refused the recording.")
                .appFont(.body)
                .foregroundStyle(.orange)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Close", action: close)
                Button("Try again") { model.start() }
            }
        }
    }
}

// MARK: - Panel

/// A floating panel that does not activate the app (so recording the
/// user's front app is not interrupted by this one).
final class DemonstrationNSPanel: NSPanel {
    var onCancel: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onCancel?() }
}

/// Owns the panel. `toggleRecording()` is what the hotkey and the menu item
/// call: it starts (or stops) and shows the panel.
@MainActor
final class DemonstrationPanelController {
    let model: DemonstrationModel
    private var panel: DemonstrationNSPanel?

    init(actions: DemonstrationActions) {
        model = DemonstrationModel(actions: actions)
    }

    var isVisible: Bool { panel?.isVisible == true }

    func toggleRecording() {
        model.toggleRecording()
        show()
    }

    func show() {
        let panel = self.panel ?? makePanel()
        self.panel = panel
        if !panel.isVisible { position(panel) }
        panel.orderFrontRegardless()
    }

    func close() {
        panel?.orderOut(nil)
    }

    private func makePanel() -> DemonstrationNSPanel {
        let panel = DemonstrationNSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 160),
            styleMask: [.titled, .closable, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered, defer: true
        )
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
        panel.onCancel = { [weak self] in self?.close() }
        let root = DemonstrationView(model: model) { [weak self] in self?.close() }
            .appTextSize()
        let hosting = NSHostingController(rootView: root)
        hosting.sizingOptions = [.preferredContentSize]
        panel.contentViewController = hosting
        return panel
    }

    /// Top-right of the screen with the mouse, out of the way of the task.
    private func position(_ panel: NSPanel) {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { panel.center(); return }
        panel.setFrameTopLeftPoint(NSPoint(x: frame.maxX - panel.frame.width - 16,
                                           y: frame.maxY - 16))
    }
}
