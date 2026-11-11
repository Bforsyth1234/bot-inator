import ServiceManagement
import SwiftUI

/// Settings section for the app itself (#96): the bundled daemon's state,
/// start at login (off by default) and the new-version check (off by
/// default). Embedded in `SettingsView` as `AppLifecycleSettingsView()`.
struct AppLifecycleSettingsView: View {
    @ObservedObject private var launcher = DaemonLauncher.shared
    @ObservedObject private var updates = UpdateChecker.shared
    @State private var loginStatus = SMAppService.mainApp.status
    @State private var loginError: String?

    var body: some View {
        Section("App") {
            if launcher.isBundled {
                daemonRow
            }
            Toggle("Start bot-inator at login", isOn: Binding(
                get: { loginStatus == .enabled || loginStatus == .requiresApproval },
                set: { setLogin($0) }
            ))
            if loginStatus == .requiresApproval {
                HStack {
                    Text("macOS needs your OK in System Settings > General > Login Items.")
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Open…") { SMAppService.openSystemSettingsLoginItems() }
                }
            }
            if let loginError {
                Label(loginError, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
                    .appFont(.secondary)
            }

            Toggle("Check for new versions", isOn: Binding(
                get: { updates.enabled },
                set: { updates.enabled = $0 }
            ))
            Text("Once a day, asks GitHub for the latest release (GET api.github.com/repos/Bforsyth1234/bot-inator/releases/latest). Sends nothing about you or this Mac beyond the request itself; GitHub sees your IP address. It runs in the app, so it isn't in the privacy ledger.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                updateStatus
                Spacer()
                Button(updates.checking ? "Checking…" : "Check now") {
                    Task { await updates.check(userInitiated: true) }
                }
                .disabled(updates.checking)
            }
        }
        .onAppear { loginStatus = SMAppService.mainApp.status }
    }

    @ViewBuilder private var daemonRow: some View {
        HStack {
            switch launcher.state {
            case .running:
                Label("Assistant running", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            case .external:
                Label("Using a daemon started outside the app", systemImage: "terminal")
                    .foregroundStyle(.secondary)
            case .checking:
                Label("Starting the assistant…", systemImage: "hourglass")
                    .foregroundStyle(.secondary)
            case let .restarting(attempt, delay):
                Label("Assistant stopped; restarting in \(delay) s (try \(attempt))",
                      systemImage: "arrow.clockwise")
                    .foregroundStyle(.orange)
            case .failed:
                Label("The assistant couldn't start", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            case .stopped, .notBundled:
                Label("Assistant stopped", systemImage: "stop.circle")
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if case .failed = launcher.state {
                Button("Try again") { launcher.retry() }
            }
            Button("Show log") { launcher.showLog() }
        }
        .appFont(.secondary)
    }

    @ViewBuilder private var updateStatus: some View {
        if updates.updateAvailable, let latest = updates.latestVersion {
            Link("bot-inator \(latest) is available: open the release page",
                 destination: updates.latestURL ?? UpdateChecker.releasesPage)
                .appFont(.secondary)
        } else if let error = updates.lastError {
            Text(error).appFont(.secondary).foregroundStyle(.secondary)
        } else if let checked = updates.lastChecked {
            Text("Up to date (\(updates.currentVersion)), checked \(checked.formatted(date: .omitted, time: .shortened))")
                .appFont(.secondary).foregroundStyle(.secondary)
        } else {
            Text("Version \(updates.currentVersion)")
                .appFont(.secondary).foregroundStyle(.secondary)
        }
    }

    private func setLogin(_ on: Bool) {
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
            loginError = nil
        } catch {
            loginError = "Couldn't change the login item: \(error.localizedDescription)"
        }
        loginStatus = SMAppService.mainApp.status
    }
}
