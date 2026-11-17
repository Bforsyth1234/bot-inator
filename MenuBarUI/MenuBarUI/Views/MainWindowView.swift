import AppKit
import SwiftUI

/// Preferences for the main window, kept in `UserDefaults` like the text size.
enum MainWindowPrefs {
    /// Open the main window when the app starts (on by default).
    static let openAtLaunchKey = "mainWindow.openAtLaunch"
    /// Keep the Dock icon and Cmd-Tab entry even with no window open (off by
    /// default: the app goes back to menu-bar-only when its windows close).
    static let alwaysInDockKey = "mainWindow.alwaysInDock"

    static var openAtLaunch: Bool {
        UserDefaults.standard.object(forKey: openAtLaunchKey) as? Bool ?? true
    }

    static var alwaysInDock: Bool {
        UserDefaults.standard.bool(forKey: alwaysInDockKey)
    }
}

/// One page in the main window's sidebar.
enum MainSection: String, CaseIterable, Identifiable, Hashable {
    case thoughts, inbox, chat, tools, watches, rules, history, privacy, settings, debug

    var id: String { rawValue }

    var title: String {
        switch self {
        case .thoughts: return "Thoughts"
        case .inbox: return "Inbox"
        case .chat: return "Chat"
        case .tools: return "Tools"
        case .watches: return "Watches"
        case .rules: return "Standing rules"
        case .history: return "History"
        case .privacy: return "Privacy ledger"
        case .settings: return "Settings"
        case .debug: return "Debug"
        }
    }

    var symbol: String {
        switch self {
        case .thoughts: return "brain"
        case .inbox: return "tray"
        case .chat: return "bubble.left.and.bubble.right"
        case .tools: return "hammer"
        case .watches: return "eye"
        case .rules: return "checklist"
        case .history: return "clock.arrow.circlepath"
        case .privacy: return "network"
        case .settings: return "slider.horizontal.3"
        case .debug: return "ladybug"
        }
    }

    /// Sidebar groups, top to bottom.
    static let groups: [(title: String, sections: [MainSection])] = [
        ("Assistant", [.thoughts, .inbox, .chat]),
        ("Automation", [.tools, .watches, .rules, .history]),
        ("App", [.privacy, .settings, .debug]),
    ]
}

/// The full app window: thoughts, inbox, chat, tools, settings and the rest
/// in one sidebar window. Opens from the menu bar icon, the Dock and launch;
/// shows in the Dock and Cmd-Tab while open (``MainWindowController``).
struct MainWindowView: View {
    @EnvironmentObject private var delivery: DeliveryViewModel
    /// #84 "Watch me", the same toggle the menu bar panel uses.
    var watchMe: () -> Void = {}

    @EnvironmentObject private var router: MainWindowRouter
    @State private var columnVisibility: NavigationSplitViewVisibility = .all

    private var selection: Binding<MainSection?> {
        Binding(
            get: { router.section },
            set: { router.section = $0 ?? .thoughts }
        )
    }

    var body: some View {
        NavigationSplitView(columnVisibility: $columnVisibility) {
            sidebar
                .navigationSplitViewColumnWidth(min: 190, ideal: 210, max: 280)
        } detail: {
            VStack(spacing: 0) {
                NoticeBanner()
                detail(for: selection.wrappedValue ?? .thoughts)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .navigationTitle((selection.wrappedValue ?? .thoughts).title)
            .toolbar { toolbar }
        }
        .frame(minWidth: 760, minHeight: 520)
        .task { await delivery.refresh() }
    }

    // MARK: Sidebar

    private var sidebar: some View {
        VStack(spacing: 0) {
            List(selection: selection) {
                ForEach(MainSection.groups, id: \.title) { group in
                    Section(group.title) {
                        ForEach(group.sections) { section in
                            sidebarRow(section).tag(section)
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            Divider()
            SidebarStatusFooter(watchMe: watchMe)
        }
    }

    @ViewBuilder
    private func sidebarRow(_ section: MainSection) -> some View {
        if section == .inbox {
            Label(section.title, systemImage: section.symbol)
                .badge(delivery.inboxCount)
        } else {
            Label(section.title, systemImage: section.symbol)
        }
    }

    // MARK: Detail

    @ViewBuilder
    private func detail(for section: MainSection) -> some View {
        switch section {
        case .thoughts:
            ThoughtStreamView()
        case .inbox:
            InboxTab()
        case .chat:
            ChatView()
        case .tools:
            ToolManagerView()
        case .watches:
            formPage { Section { WatchesView() } }
        case .rules:
            formPage { Section { RulesView() } }
        case .history:
            formPage { Section { ActionHistoryView() } }
        case .privacy:
            formPage { PrivacyLedgerView() }
        case .settings:
            SettingsView()
        case .debug:
            DebugView()
        }
    }

    /// Views written as Settings sections, shown on their own page.
    private func formPage<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        Form { content() }
            .formStyle(.grouped)
    }

    // MARK: Toolbar

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            PauseMenu(viewModel: delivery)
                .help("Pause or resume offers")
            Button {
                watchMe()
            } label: {
                Label("Watch me", systemImage: "record.circle")
            }
            .help("Record yourself doing a task once (⌃⌥⌘R), then make a tool from it")
            Menu {
                MeetingRecorderMenuItems()
            } label: {
                Label("Meeting notes", systemImage: "waveform")
            }
            .help("Take meeting notes")
        }
    }
}

/// Bottom of the sidebar: connection state and what left the Mac today.
private struct SidebarStatusFooter: View {
    @EnvironmentObject private var ws: WebSocketManager
    var watchMe: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                Text(statusText)
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            if !ws.isConnected, let hint = ws.authHint {
                Text(hint)
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            PrivacyLedgerTodayLabel()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var statusText: String {
        if !ws.isConnected { return "Daemon offline" }
        if ws.lastStatus?.paused == true { return "Connected · paused" }
        return "Connected"
    }

    private var statusColor: Color {
        if !ws.isConnected { return .red }
        if ws.lastStatus?.paused == true { return .orange }
        return .green
    }
}

/// Toggles for the main window, shown in Settings > App.
struct MainWindowSettingsView: View {
    @AppStorage(MainWindowPrefs.openAtLaunchKey) private var openAtLaunch = true
    @AppStorage(MainWindowPrefs.alwaysInDockKey) private var alwaysInDock = false

    var body: some View {
        Section("Main window") {
            Toggle("Open the main window when bot-inator starts", isOn: $openAtLaunch)
            Toggle("Keep bot-inator in the Dock when no window is open", isOn: $alwaysInDock)
                .onChange(of: alwaysInDock) { _, _ in
                    NotificationCenter.default.post(name: .activationPolicyPrefChanged, object: nil)
                }
            Text("Click the menu bar icon to open this window; right-click it for Pause, Watch me and Quit.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
        }
    }
}

extension Notification.Name {
    static let activationPolicyPrefChanged = Notification.Name("botinator.activationPolicyPrefChanged")
}
