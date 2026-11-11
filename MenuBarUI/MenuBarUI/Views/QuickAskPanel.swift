import AppKit
import ApplicationServices
import Combine
import SwiftUI

/// Sends one chat turn with optional quick-ask context and returns its
/// `message_id`. The app wires it to `WebSocketManager.sendUserMessage(_:context:)`.
typealias QuickAskSend = @MainActor (String, ChatContextPayload?) -> String

// MARK: - Reading the screen

/// What was in front when the hotkey fired (#74). Read once, before the
/// panel shows; the selection is kept only until the panel closes.
struct ScreenContext: Equatable {
    var frontApp: String?
    var windowTitle: String?
    /// The focused element's selected text (Accessibility only).
    var selection: String?
    /// `AXIsProcessTrusted()` for this app when the context was read.
    var accessibilityTrusted: Bool

    static let empty = ScreenContext(accessibilityTrusted: false)
}

enum ScreenContextReader {
    /// Never prompts: `AXIsProcessTrusted` only checks.
    static func isTrusted() -> Bool { AXIsProcessTrusted() }

    /// The front app (unless it is this app), its window title, and with
    /// Accessibility the selected text. Must run before the panel shows.
    static func capture() -> ScreenContext {
        let trusted = isTrusted()
        var ctx = ScreenContext(accessibilityTrusted: trusted)
        guard let app = NSWorkspace.shared.frontmostApplication,
              app.bundleIdentifier != Bundle.main.bundleIdentifier else { return ctx }
        ctx.frontApp = app.localizedName
        let pid = app.processIdentifier
        if trusted {
            let element = AXUIElementCreateApplication(pid)
            // A hung app must not hang the hotkey.
            AXUIElementSetMessagingTimeout(element, 0.3)
            if let window: AXUIElement = attribute(element, kAXFocusedWindowAttribute) {
                ctx.windowTitle = nonEmpty(attribute(window, kAXTitleAttribute) as String?)
            }
            if let focused: AXUIElement = attribute(element, kAXFocusedUIElementAttribute) {
                ctx.selection = nonEmpty(attribute(focused, kAXSelectedTextAttribute) as String?)
            }
        }
        if ctx.windowTitle == nil {
            ctx.windowTitle = windowListTitle(pid: pid)
        }
        return ctx
    }

    /// Put this app in System Settings → Accessibility (a user click only).
    static func requestAccess() {
        let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        if !AXIsProcessTrustedWithOptions(options) {
            openAccessibilitySettings()
        }
    }

    static func openAccessibilitySettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    private static func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success,
              let value else { return nil }
        if T.self == AXUIElement.self {
            guard CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
            return (value as! T)
        }
        return value as? T
    }

    /// Window title from the window list (needs Screen Recording for other
    /// apps' titles; nil without it).
    private static func windowListTitle(pid: pid_t) -> String? {
        let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
            return nil
        }
        for info in list {
            guard (info[kCGWindowOwnerPID as String] as? pid_t) == pid,
                  (info[kCGWindowLayer as String] as? Int) == 0 else { continue }
            if let name = nonEmpty(info[kCGWindowName as String] as? String) { return name }
        }
        return nil
    }

    private static func nonEmpty(_ s: String?) -> String? {
        guard let s, !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return s
    }
}

// MARK: - Model

@MainActor
final class QuickAskModel: ObservableObject {
    @Published var draft = ""
    @Published private(set) var screen = ScreenContext.empty
    @Published var includeWindow = true
    @Published var includeSelection = false
    /// Set only when the user clicks "Use what's on the clipboard".
    @Published private(set) var clipboardText: String?
    @Published private(set) var askedText: String?
    @Published private(set) var reply: String?
    @Published private(set) var progress: String?
    @Published private(set) var isPending = false

    private let ws: WebSocketManager
    private let send: QuickAskSend
    private var messageId: String?
    private var scanned = 0
    private var cancellable: AnyCancellable?

    init(ws: WebSocketManager, send: @escaping QuickAskSend) {
        self.ws = ws
        self.send = send
        cancellable = ws.$messages
            .receive(on: RunLoop.main)
            .sink { [weak self] messages in
                MainActor.assumeIsolated { self?.ingest(messages) }
            }
    }

    var isConnected: Bool { ws.isConnected }

    /// True when the main engine is a cloud one and personal text is not
    /// allowed: the daemon keeps the included text on this Mac (#31/7c).
    var selectionStaysLocal: Bool {
        guard let status = ws.lastStatus,
              let main = status.engineProviders?["main"] else { return false }
        return main != "local" && status.cloudPersonalText != true
    }

    /// The text that would go with the message, if any.
    var includedText: (text: String, source: ChatContextPayload.SelectionSource)? {
        guard includeSelection else { return nil }
        if let s = screen.selection { return (s, .selection) }
        if let c = clipboardText { return (c, .clipboard) }
        return nil
    }

    /// A fresh ask: new screen context, nothing carried from the last one.
    func reset(screen: ScreenContext) {
        self.screen = screen
        draft = ""
        includeWindow = true
        // Selecting text and pressing the hotkey is the choice; the toggle
        // stays visible to take it back.
        includeSelection = screen.selection != nil
        clipboardText = nil
        askedText = nil
        reply = nil
        progress = nil
        isPending = false
        messageId = nil
    }

    func useClipboard() {
        guard let text = NSPasteboard.general.string(forType: .string),
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            clipboardText = nil
            includeSelection = false
            return
        }
        clipboardText = text
        includeSelection = true
    }

    func dropIncludedText() {
        clipboardText = nil
        includeSelection = false
    }

    func submit() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isPending else { return }
        var ctx = ChatContextPayload()
        if includeWindow {
            ctx.frontApp = screen.frontApp
            ctx.windowTitle = screen.windowTitle
        }
        if let included = includedText {
            ctx.selection = included.text
            ctx.selectionSource = included.source
        }
        scanned = ws.messages.count
        messageId = send(text, ctx.isEmpty ? nil : ctx)
        askedText = text
        reply = nil
        progress = "thinking…"
        isPending = true
        draft = ""
        // The selection was for this one message.
        screen.selection = nil
        clipboardText = nil
        includeSelection = false
    }

    private func ingest(_ messages: [WSMessage]) {
        if messages.count < scanned { scanned = 0 }
        guard let messageId, scanned < messages.count else {
            scanned = messages.count
            return
        }
        for msg in messages[scanned...] {
            guard case let .thought(_, _, payload) = msg, payload.eventId == messageId else { continue }
            switch payload.stage {
            case .complete:
                reply = payload.content
                progress = nil
                isPending = false
            case .plan:
                progress = "planning…"
            case .toolResult:
                progress = "working…"
            default:
                break
            }
        }
        scanned = messages.count
    }
}

// MARK: - View

struct QuickAskView: View {
    @ObservedObject var model: QuickAskModel
    var close: () -> Void
    @Environment(\.textSize) private var textSize
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            prompt
            contextRows
            if model.askedText != nil { answer }
        }
        .padding(14)
        .frame(width: 580 * textSize.layoutScale, alignment: .leading)
        .onAppear { focused = true }
    }

    private var prompt: some View {
        HStack(spacing: 8) {
            Image(systemName: "brain.head.profile")
                .appFont(.title3)
                .foregroundStyle(Color.accentColor)
            TextField(model.screen.frontApp.map { "Ask about \($0)…" } ?? "Ask the agent…",
                      text: $model.draft, axis: .vertical)
                .textFieldStyle(.plain)
                .appFont(.title3)
                .lineLimit(1...4)
                .focused($focused)
                .onSubmit { model.submit() }
            if model.isPending {
                ProgressView().controlSize(.small)
            }
            Button(action: close) {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Close (Esc)")
        }
    }

    @ViewBuilder
    private var contextRows: some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.screen.frontApp != nil {
                Toggle(isOn: $model.includeWindow) {
                    Text(windowLabel).lineLimit(1).truncationMode(.middle)
                }
                .toggleStyle(.checkbox)
                .appFont(.secondary)
            }
            selectionRow
            if model.includedText != nil, model.selectionStaysLocal {
                Text("The main engine is a cloud one: the included text stays on this Mac unless you allow personal text in Settings.")
                    .appFont(.fine)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !model.isConnected {
                Text("Daemon offline: the message can't be sent.")
                    .appFont(.fine)
                    .foregroundStyle(.red)
            }
        }
    }

    private var windowLabel: String {
        let app = model.screen.frontApp ?? ""
        if let title = model.screen.windowTitle { return "Include \(app) — \(title)" }
        return "Include \(app)"
    }

    @ViewBuilder
    private var selectionRow: some View {
        if let selection = model.screen.selection {
            Toggle(isOn: $model.includeSelection) {
                Text("Include the selected text (\(selection.count) characters)")
            }
            .toggleStyle(.checkbox)
            .appFont(.secondary)
        } else if let clip = model.clipboardText {
            HStack(spacing: 6) {
                Image(systemName: "doc.on.clipboard").foregroundStyle(.secondary)
                Text("Clipboard included (\(clip.count) characters)")
                Button("Don't include") { model.dropIncludedText() }
                    .buttonStyle(.link)
            }
            .appFont(.secondary)
        } else if model.askedText == nil {
            HStack(spacing: 6) {
                if !model.screen.accessibilityTrusted {
                    Text("Selected text needs Accessibility.")
                        .foregroundStyle(.secondary)
                    Button("Allow…") { ScreenContextReader.requestAccess() }
                        .buttonStyle(.link)
                    Text("·").foregroundStyle(.secondary)
                }
                Button("Use what's on the clipboard?") { model.useClipboard() }
                    .buttonStyle(.link)
            }
            .appFont(.secondary)
        }
    }

    private var answer: some View {
        VStack(alignment: .leading, spacing: 6) {
            Divider()
            if let asked = model.askedText {
                Text(asked)
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            ScrollView {
                Group {
                    if let reply = model.reply {
                        Text(reply)
                            .textSelection(.enabled)
                    } else {
                        Text(model.progress ?? "thinking…")
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxHeight: 320 * textSize.layoutScale)
            Text("Tool calls still ask for approval. The exchange is also in Chat.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Panel

/// A floating panel that takes keystrokes without activating the app, so
/// the app the user was in stays frontmost.
final class QuickAskNSPanel: NSPanel {
    var onCancel: (() -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func cancelOperation(_ sender: Any?) { onCancel?() }
}

/// Owns the quick-ask panel: `toggle()` is what the hotkey calls.
@MainActor
final class QuickAskPanelController {
    private let model: QuickAskModel
    private var panel: QuickAskNSPanel?

    init(ws: WebSocketManager, send: @escaping QuickAskSend) {
        model = QuickAskModel(ws: ws, send: send)
    }

    var isVisible: Bool { panel?.isVisible == true }

    func toggle() {
        if isVisible { close() } else { show() }
    }

    func show() {
        // Read the screen before the panel takes key focus.
        model.reset(screen: ScreenContextReader.capture())
        let panel = self.panel ?? makePanel()
        self.panel = panel
        position(panel)
        panel.makeKeyAndOrderFront(nil)
    }

    func close() {
        panel?.orderOut(nil)
        // Nothing from this ask outlives the panel.
        model.reset(screen: .empty)
    }

    private func makePanel() -> QuickAskNSPanel {
        let panel = QuickAskNSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 580, height: 120),
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
        let root = QuickAskView(model: model) { [weak self] in self?.close() }
            .appTextSize()
        let hosting = NSHostingController(rootView: root)
        hosting.sizingOptions = [.preferredContentSize]
        panel.contentViewController = hosting
        return panel
    }

    /// Upper third of the screen with the mouse, like Spotlight.
    private func position(_ panel: NSPanel) {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let frame = screen?.visibleFrame else { panel.center(); return }
        let width = panel.frame.width
        panel.setFrameTopLeftPoint(NSPoint(x: frame.midX - width / 2,
                                           y: frame.minY + frame.height * 0.75))
    }
}
