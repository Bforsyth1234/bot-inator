import AppKit
import Combine
import SwiftUI

@main
struct MenuBarUIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        // Every window is AppKit-owned (the main window, approval cards, the
        // panels), so the app needs no SwiftUI window scene. This Settings
        // scene is never shown: ⌘, opens the main window's Settings page.
        Settings { EmptyView() }
            .commands {
                CommandGroup(replacing: .appSettings) {
                    Button("Settings…") { delegate.showMainWindow(section: .settings) }
                        .keyboardShortcut(",", modifiers: [.command])
                }
                CommandGroup(before: .windowList) {
                    Button("bot-inator") { delegate.showMainWindow() }
                        .keyboardShortcut("b", modifiers: [.command, .shift])
                    Button("Chat") { delegate.showMainWindow(section: .chat) }
                        .keyboardShortcut("c", modifiers: [.command, .shift])
                    Button("Manage Tools") { delegate.showMainWindow(section: .tools) }
                        .keyboardShortcut("t", modifiers: [.command, .shift])
                    Divider()
                }
            }
    }
}

/// The Inbox tab (#87): offers over today's cap and offers held for later.
struct InboxTab: View {
    @EnvironmentObject private var delivery: DeliveryViewModel

    var body: some View {
        ScrollView {
            OfferInboxView(viewModel: delivery)
                .padding(12)
        }
        .task { await delivery.refresh() }
    }
}

/// Owns the WebSocket manager and approval presenter for the full app
/// lifetime so thoughts and approvals surface without requiring the popover
/// to be open.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    let ws = WebSocketManager()
    /// #87: one delivery model (Pause, Inbox, settings) shared by the menu,
    /// the Inbox tab, Settings and Debug.
    let delivery = DeliveryViewModel()
    private var deliveryRefresh: AnyCancellable?
    private let approvalPresenter = ApprovalPresenter()
    /// Quick-ask panel (#74), opened by the global hotkey.
    private var quickAsk: QuickAskPanelController?
    /// #84 "Watch me": the recording panel, the record hotkey and the menu
    /// item all go through this controller.
    lazy var demonstration: DemonstrationPanelController = {
        let ws = self.ws
        return DemonstrationPanelController(actions: DemonstrationActions(
            start: { ws.sendCommand(.demonstrationStart) },
            stop: { ws.sendCommand(.demonstrationStop) },
            cancel: { ws.sendCommand(.demonstrationCancel) },
            draft: { ws.sendDemonstrationDraft($0) }
        ))
    }()

    /// The main window (sidebar app window) and the menu bar icon.
    private lazy var mainWindow = MainWindowController { [unowned self] in
        AnyView(
            MainWindowView(watchMe: { [unowned self] in self.demonstration.toggleRecording() })
                .environmentObject(self.ws)
                .environmentObject(self.delivery)
                .environmentObject(self.mainWindowRouter)
                .appTextSize()
        )
    }
    let mainWindowRouter = MainWindowRouter()
    private var statusItem: StatusItemController?

    /// Bring the main window forward, optionally on one sidebar page.
    func showMainWindow(section: MainSection? = nil) {
        if let section { mainWindowRouter.section = section }
        mainWindow.show()
    }

    /// Clicking the Dock icon with no visible window reopens the main window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showMainWindow() }
        return true
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // #96: the release app starts its bundled daemon unless one already
        // answers on 8000 (./run.sh); a dev build has none and skips this.
        DaemonLauncher.shared.start()
        // #96: the new-version check, only when turned on in Settings.
        UpdateChecker.shared.startIfEnabled()
        ws.connect()
        approvalPresenter.attach(to: ws)
        // The daemon sends a status frame whenever Pause, held offers or the
        // Inbox change; refresh the shared delivery model then.
        let delivery = self.delivery
        deliveryRefresh = ws.$lastStatus
            .compactMap { $0 }
            .removeDuplicates()
            .sink { _ in Task { await delivery.refresh() } }
        observeWindowsForActivationPolicy()
        if MainWindowPrefs.alwaysInDock {
            NSApp.setActivationPolicy(.regular)
        }
        let ws = self.ws
        let quickAsk = QuickAskPanelController(ws: ws) { text, context in
            ws.sendUserMessage(text, context: context)
        }
        self.quickAsk = quickAsk
        QuickAskHotKeyController.shared.start { quickAsk.toggle() }
        // #84: follow demonstration_trace frames; ⌃⌥⌘R (default) toggles.
        let demonstration = self.demonstration
        demonstration.model.attach(messages: ws.$messages.eraseToAnyPublisher()) {
            if case let .demonstrationTrace(_, _, payload) = $0 { return payload }
            return nil
        }
        RecordHotKeyController.shared.start { [demonstration] in demonstration.toggleRecording() }
        // #86: the recorder exists from launch (it purges leftover audio).
        _ = MeetingRecorder.shared
        // Left-click the menu bar icon: the main window. Right-click: a menu.
        statusItem = StatusItemController(
            ws: ws, demo: demonstration.model, delivery: delivery,
            openMain: { [unowned self] section in self.showMainWindow(section: section) },
            watchMe: { [demonstration] in demonstration.toggleRecording() }
        )
        if MainWindowPrefs.openAtLaunch {
            showMainWindow()
        }
        // #93: the engine choice on a fresh install (no settings.json yet).
        FirstRunWindowController.shared.showIfPending()
    }

    /// #96: stop the daemon this app started (nothing if it didn't start one).
    func applicationWillTerminate(_ notification: Notification) {
        DaemonLauncher.shared.stop()
    }

    // MARK: Cmd-Tab / Dock while a window is open

    /// ``LSUIElement`` keeps the app out of Cmd-Tab and the Dock. While one
    /// of its regular windows (the main window, approval cards) is open, switch to ``.regular`` so the user
    /// can Cmd-Tab back to it; go back to ``.accessory`` when the last one
    /// closes.
    private func observeWindowsForActivationPolicy() {
        let center = NotificationCenter.default
        for name in [NSWindow.didBecomeKeyNotification,
                     NSWindow.didBecomeMainNotification,
                     NSWindow.didDeminiaturizeNotification] {
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.updateActivationPolicy(closing: nil) }
            }
        }
        center.addObserver(forName: .activationPolicyPrefChanged, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.updateActivationPolicy(closing: nil) }
        }
        center.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] note in
            let closing = note.object as? NSWindow
            MainActor.assumeIsolated { self?.updateActivationPolicy(closing: closing) }
        }
    }

    private func updateActivationPolicy(closing: NSWindow?) {
        let hasAppWindow = NSApp.windows.contains { window in
            window !== closing && window.isVisible && Self.isAppWindow(window)
        }
        let wanted: NSApplication.ActivationPolicy =
            (hasAppWindow || MainWindowPrefs.alwaysInDock) ? .regular : .accessory
        guard NSApp.activationPolicy() != wanted else { return }
        NSApp.setActivationPolicy(wanted)
        if wanted == .regular {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    /// A normal titled window, not the menu bar panel or status item.
    private static func isAppWindow(_ window: NSWindow) -> Bool {
        let name = NSStringFromClass(type(of: window))
        if name.contains("StatusBar") { return false }
        if window is NSPanel { return false }
        return window.styleMask.contains(.titled)
    }
}
