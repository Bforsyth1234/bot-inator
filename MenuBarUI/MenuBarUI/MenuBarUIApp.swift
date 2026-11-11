import AppKit
import Combine
import SwiftUI

@main
struct MenuBarUIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            RootPopoverView()
                .environmentObject(delegate.ws)
                .environmentObject(delegate.delivery)
                .appTextSize()
        } label: {
            MenuBarIconLabel(ws: delegate.ws)
        }
        .menuBarExtraStyle(.window)

        Window("Manage Tools", id: "tool-manager") {
            ToolManagerView()
                .appTextSize()
        }
        .defaultSize(width: 540, height: 540)
        .keyboardShortcut("t", modifiers: [.command, .shift])

        Window("Chat", id: "chat") {
            ChatView()
                .environmentObject(delegate.ws)
                .appTextSize()
        }
        .defaultSize(width: 560, height: 660)
        .keyboardShortcut("c", modifiers: [.command, .shift])

        Window("Settings", id: "settings") {
            SettingsView()
                .environmentObject(delegate.delivery)
                .appTextSize()
        }
        .defaultSize(width: 540, height: 560)
        .keyboardShortcut(",", modifiers: [.command])

        // The menu bar panel closes whenever it loses focus (macOS behaviour
        // for MenuBarExtra panels); this is the same content in a normal
        // window that stays open beside other apps.
        Window("bot-inator", id: "activity") {
            ActivityWindowView()
                .environmentObject(delegate.ws)
                .environmentObject(delegate.delivery)
                .appTextSize()
        }
        .defaultSize(width: 500, height: 680)
        .keyboardShortcut("b", modifiers: [.command, .shift])
    }
}

/// The menu bar icon: a pause glyph while paused, and the Inbox count
/// (#87) as a badge. Reads the status frames the daemon sends on change.
struct MenuBarIconLabel: View {
    @ObservedObject var ws: WebSocketManager

    var body: some View {
        let inbox = ws.lastStatus?.inboxCount ?? 0
        HStack(spacing: 2) {
            // Brain/robot SF Symbol for the menu bar icon.
            Image(systemName: ws.lastStatus?.paused == true ? "pause.circle" : "brain.head.profile")
            if inbox > 0 {
                Text("\(inbox)")
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

/// Thoughts + Debug in a regular, resizable window ("Open in window").
struct ActivityWindowView: View {
    @EnvironmentObject private var delivery: DeliveryViewModel

    var body: some View {
        VStack(spacing: 0) {
            NoticeBanner()
            TabView {
                ThoughtStreamView()
                    .tabItem { Label("Thoughts", systemImage: "brain") }
                InboxTab()
                    .tabItem { Label("Inbox", systemImage: "tray") }
                    .badge(delivery.inboxCount)
                DebugView()
                    .tabItem { Label("Debug", systemImage: "ladybug") }
            }
            .frame(maxHeight: .infinity)
        }
        .frame(minWidth: 380, minHeight: 420)
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

    func applicationDidFinishLaunching(_ notification: Notification) {
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
        let ws = self.ws
        let quickAsk = QuickAskPanelController(ws: ws) { text, context in
            ws.sendUserMessage(text, context: context)
        }
        self.quickAsk = quickAsk
        QuickAskHotKeyController.shared.start { quickAsk.toggle() }
    }

    // MARK: Cmd-Tab / Dock while a window is open

    /// ``LSUIElement`` keeps the app out of Cmd-Tab and the Dock. While one
    /// of its regular windows (Chat, Settings, Manage Tools, "Open in
    /// window", approval cards) is open, switch to ``.regular`` so the user
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
        center.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { [weak self] note in
            let closing = note.object as? NSWindow
            MainActor.assumeIsolated { self?.updateActivationPolicy(closing: closing) }
        }
    }

    private func updateActivationPolicy(closing: NSWindow?) {
        let hasAppWindow = NSApp.windows.contains { window in
            window !== closing && window.isVisible && Self.isAppWindow(window)
        }
        let wanted: NSApplication.ActivationPolicy = hasAppWindow ? .regular : .accessory
        guard NSApp.activationPolicy() != wanted else { return }
        NSApp.setActivationPolicy(wanted)
        if wanted == .regular {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    /// A normal titled window, not the menu bar panel or status item.
    private static func isAppWindow(_ window: NSWindow) -> Bool {
        let name = NSStringFromClass(type(of: window))
        if name.contains("StatusBar") || name.contains("MenuBarExtra") { return false }
        if window is NSPanel { return false }
        return window.styleMask.contains(.titled)
    }
}

/// Top-level popover content with tabs for the thought stream and in-app
/// diagnostics. New diagnostic panes can be added as additional tabs.
struct RootPopoverView: View {
    @EnvironmentObject private var ws: WebSocketManager
    @EnvironmentObject private var delivery: DeliveryViewModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(spacing: 0) {
            NoticeBanner()
            TabView {
                ThoughtStreamView()
                    .tabItem { Label("Thoughts", systemImage: "brain") }
                InboxTab()
                    .tabItem { Label("Inbox", systemImage: "tray") }
                    .badge(delivery.inboxCount)
                DebugView()
                    .tabItem { Label("Debug", systemImage: "ladybug") }
            }
            // The tab view takes what's left; banners and the button bar
            // must never push each other out of the fixed-height popover.
            .frame(maxHeight: .infinity)
            .layoutPriority(-1)
            Divider()
            // #82: what left this Mac today, one line.
            HStack {
                PrivacyLedgerTodayLabel()
                Spacer()
            }
            .padding(.horizontal, 10)
            .padding(.top, 4)
            .fixedSize(horizontal: false, vertical: true)
            HStack {
                // #87: Pause / Resume for real (listeners keep running).
                PauseMenu(viewModel: delivery)
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                Spacer()
                Button {
                    presentWindow(id: "activity")
                } label: {
                    Label("Open in window", systemImage: "macwindow")
                        .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .help("Open Thoughts and Debug in a window that stays open (⇧⌘B)")
                Spacer()
                Button {
                    presentWindow(id: "chat")
                } label: {
                    Label("Chat…", systemImage: "bubble.left.and.bubble.right")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("c", modifiers: [.command, .shift])
                Spacer()
                Button {
                    presentWindow(id: "settings")
                } label: {
                    Label("Settings…", systemImage: "slider.horizontal.3")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut(",", modifiers: [.command])
                Spacer()
                Button {
                    presentWindow(id: "tool-manager")
                } label: {
                    Label("Manage Tools…", systemImage: "gear")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("t", modifiers: [.command, .shift])
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .fixedSize(horizontal: false, vertical: true)
            .layoutPriority(1)
        }
        .frame(width: 460, height: 640)
    }

    // On ``LSUIElement`` apps SwiftUI's ``openWindow`` silently no-ops when
    // called from the ``MenuBarExtra`` popover because the process is still
    // in ``.accessory`` activation. Foregrounding the app first forces the
    // scene to materialize and come to front.
    private func presentWindow(id: String) {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: id)
    }
}
