import SwiftUI

@main
struct MenuBarUIApp: App {
    @StateObject private var ws = WebSocketManager()

    var body: some Scene {
        MenuBarExtra {
            RootPopoverView()
                .environmentObject(ws)
        } label: {
            // Brain/robot SF Symbol for the menu bar icon.
            Image(systemName: "brain.head.profile")
        }
        .menuBarExtraStyle(.window)
    }
}

/// Top-level popover content. Hosts the thought stream and presents the
/// approval sheet whenever an `approval_request` is pending.
struct RootPopoverView: View {
    @EnvironmentObject private var ws: WebSocketManager
    @State private var showingApproval = false
    @State private var presentedRequestId: String?

    var body: some View {
        ThoughtStreamView()
            .sheet(isPresented: $showingApproval) {
                if let pending = ws.pendingApproval {
                    ApprovalAlert(payload: pending.payload) { approved, note in
                        ws.sendApprovalResponse(
                            requestId: pending.payload.requestId,
                            approved: approved,
                            userNote: note
                        )
                        presentedRequestId = nil
                    }
                }
            }
            .onChange(of: ws.pendingApproval?.payload.requestId) { _, newId in
                if let newId, newId != presentedRequestId {
                    presentedRequestId = newId
                    showingApproval = true
                } else if newId == nil {
                    showingApproval = false
                }
            }
    }
}
