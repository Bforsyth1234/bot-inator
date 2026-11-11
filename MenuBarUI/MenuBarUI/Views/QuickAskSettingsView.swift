import SwiftUI

/// Settings section for quick-ask (#74): the global shortcut and whether
/// this app may read the selected text (Accessibility). Embedded in
/// `SettingsView` as `QuickAskSettingsView()`.
struct QuickAskSettingsView: View {
    @AppStorage(QuickAskShortcut.storageKey) private var stored: String = QuickAskShortcut.fallback.rawValue
    @ObservedObject private var hotKeys = QuickAskHotKeyController.shared
    @State private var trusted = ScreenContextReader.isTrusted()

    var body: some View {
        Section("Quick ask") {
            Picker("Shortcut", selection: $stored) {
                ForEach(QuickAskShortcut.allCases) { shortcut in
                    Text(shortcut.label).tag(shortcut.rawValue)
                }
            }
            Text("Opens a small prompt over the app you're in. It sends the app's name and window title, and the selected text only when you leave it ticked.")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let error = hotKeys.lastError {
                HStack {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .appFont(.secondary)
                    Spacer()
                    Button("Retry") { hotKeys.retry() }
                }
            }
            HStack {
                Label(trusted ? "Can read selected text" : "Selected text needs Accessibility",
                      systemImage: trusted ? "checkmark.circle.fill" : "hand.raised")
                    .foregroundStyle(trusted ? Color.green : Color.secondary)
                    .appFont(.secondary)
                Spacer()
                if !trusted {
                    Button("Allow…") {
                        ScreenContextReader.requestAccess()
                        trusted = ScreenContextReader.isTrusted()
                    }
                }
                Button("Check") { trusted = ScreenContextReader.isTrusted() }
            }
            if !trusted {
                Text("Without it, quick-ask offers to use what's on the clipboard instead.")
                    .appFont(.fine)
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear { trusted = ScreenContextReader.isTrusted() }
    }
}
