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

        Window("Manage Tools", id: "tool-manager") {
            ToolManagerView()
        }
        .defaultSize(width: 480, height: 480)
        .keyboardShortcut("t", modifiers: [.command, .shift])

        Window("Chat", id: "chat") {
            ChatView()
                .environmentObject(delegate.ws)
        }
        .defaultSize(width: 520, height: 620)
        .keyboardShortcut("c", modifiers: [.command, .shift])
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
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(spacing: 0) {
            TabView {
                ThoughtStreamView()
                    .tabItem { Label("Thoughts", systemImage: "brain") }
                DebugView()
                    .tabItem { Label("Debug", systemImage: "ladybug") }
            }
            Divider()
            HStack {
                Button {
                    openWindow(id: "chat")
                } label: {
                    Label("Chat…", systemImage: "bubble.left.and.bubble.right")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("c", modifiers: [.command, .shift])
                Spacer()
                Button {
                    openWindow(id: "tool-manager")
                } label: {
                    Label("Manage Tools…", systemImage: "gear")
                        .labelStyle(.titleAndIcon)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut("t", modifiers: [.command, .shift])
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
        }
        .frame(width: 420, height: 600)
    }
}
