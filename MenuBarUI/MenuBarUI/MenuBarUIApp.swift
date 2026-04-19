import AppKit
import SwiftUI

@main
struct MenuBarUIApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            RootPopoverView()
                .environmentObject(delegate.ws)
        } label: {
            // Brain/robot SF Symbol for the menu bar icon.
            Image(systemName: "brain.head.profile")
        }
        .menuBarExtraStyle(.window)
    }
}

/// Owns the WebSocket manager and approval presenter for the full app
/// lifetime so thoughts and approvals surface without requiring the popover
/// to be open.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, ObservableObject {
    let ws = WebSocketManager()
    private let approvalPresenter = ApprovalPresenter()

    func applicationDidFinishLaunching(_ notification: Notification) {
        ws.connect()
        approvalPresenter.attach(to: ws)
    }
}

/// Top-level popover content with tabs for the thought stream and in-app
/// diagnostics. New diagnostic panes can be added as additional tabs.
struct RootPopoverView: View {
    @EnvironmentObject private var ws: WebSocketManager

    var body: some View {
        TabView {
            ThoughtStreamView()
                .tabItem { Label("Thoughts", systemImage: "brain") }
            DebugView()
                .tabItem { Label("Debug", systemImage: "ladybug") }
        }
        .frame(width: 420, height: 560)
    }
}
