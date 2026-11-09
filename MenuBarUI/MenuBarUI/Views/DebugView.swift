import SwiftUI

/// In-app diagnostics surfaced alongside the thought stream. Reads directly
/// from `WebSocketManager.debug` and status payloads so there is no separate
/// pipeline to keep in sync.
struct DebugView: View {
    @EnvironmentObject private var ws: WebSocketManager

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                connectionSection
                providersSection
                listenerHealthSection
                countersSection
                stagesSection
                errorsSection
                placeholderSection
            }
            .padding(12)
        }
        .frame(width: 420, height: 520)
    }

    // MARK: - Sections

    private var connectionSection: some View {
        section(title: "Connection") {
            row("Connected", ws.isConnected ? "yes" : "no")
            if let status = ws.lastStatus {
                row("Daemon state", status.state.rawValue)
                if let model = status.modelLoaded { row("Model", model) }
                if let listeners = status.listenersActive {
                    row("Listeners", listeners.joined(separator: ", "))
                }
                if let version = status.version { row("Version", version) }
            }
            row("Connect attempts", "\(ws.debug.connectAttempts)")
            row("Reconnects scheduled", "\(ws.debug.reconnectsScheduled)")
            if let err = ws.debug.lastConnectError {
                row("Last connect error", err, mono: true)
            }
        }
    }

    /// Which provider each engine runs on, and whether personal text is
    /// kept off cloud engines (#31).
    private var providersSection: some View {
        section(title: "Engines") {
            if let providers = ws.lastStatus?.engineProviders, !providers.isEmpty {
                ForEach(["main", "analysis", "drafting"], id: \.self) { role in
                    if let provider = providers[role] {
                        row(role, provider == "local" ? "local (on this Mac)" : "\(provider) (cloud)")
                    }
                }
                if providers.values.contains(where: { $0 != "local" }) {
                    row("Personal text to cloud",
                        ws.lastStatus?.cloudPersonalText == true
                            ? "sent (AGENT_CLOUD_PERSONAL_TEXT=1)"
                            : "kept on this Mac")
                }
            } else {
                Text("Not reported by the daemon.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// Liveness of supervised listeners (#16/#19).
    private var listenerHealthSection: some View {
        section(title: "Listener health") {
            if let health = ws.lastStatus?.listenerHealth, !health.isEmpty {
                ForEach(health.keys.sorted(), id: \.self) { name in
                    if let entry = health[name] {
                        row(name, entry.alive == true ? "alive" : "down")
                        row("  restarts", "\(entry.restartCount ?? 0)")
                        if let pid = entry.pid { row("  pid", "\(pid)") }
                        if let err = entry.lastError, !err.isEmpty {
                            row("  last error", err, mono: true)
                        }
                    }
                }
            } else {
                Text("No supervised listeners running.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var countersSection: some View {
        section(title: "Frames") {
            row("Received", "\(ws.debug.framesReceived)")
            row("Decoded", "\(ws.debug.framesDecoded)")
            row("Decode errors", "\(ws.debug.decodeErrorCount)")
            ForEach(sortedFrameTypes(), id: \.0) { key, count in
                row("  \(key)", "\(count)")
            }
        }
    }

    private var stagesSection: some View {
        section(title: "Thought stages") {
            if ws.debug.thoughtsByStage.isEmpty {
                Text("No thoughts received yet.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(sortedStages(), id: \.0) { stage, count in
                    row(stage.rawValue, "\(count)")
                }
            }
        }
    }

    private var errorsSection: some View {
        section(title: "Recent decode errors") {
            if ws.debug.recentDecodeErrors.isEmpty {
                Text("None.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(ws.debug.recentDecodeErrors.reversed()) { entry in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(Self.timeFormatter.string(from: entry.timestamp))
                            .font(.caption2.monospaced())
                            .foregroundStyle(.secondary)
                        Text(entry.message)
                            .font(.caption.monospaced())
                            .textSelection(.enabled)
                        if let raw = entry.rawPreview {
                            Text(raw)
                                .font(.caption2.monospaced())
                                .foregroundStyle(.secondary)
                                .lineLimit(3)
                                .textSelection(.enabled)
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
            if let raw = ws.debug.lastRawFrame {
                Divider()
                Text("Last raw frame:")
                    .font(.caption.weight(.semibold))
                Text(raw)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(4)
                    .textSelection(.enabled)
            }
            HStack {
                Spacer()
                Button("Reset stats") { ws.resetDebugStats() }
                    .buttonStyle(.borderless)
                    .font(.caption)
            }
        }
    }

    private var placeholderSection: some View {
        section(title: "More diagnostics") {
            Text("Reserved for upcoming tools (daemon log tail, manual event injection).")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Helpers

    private func sortedFrameTypes() -> [(String, Int)] {
        ws.debug.framesByType.sorted { $0.key < $1.key }
    }

    private func sortedStages() -> [(ThoughtStage, Int)] {
        let order: [ThoughtStage] = [
            .eventReceived, .analysis, .memory, .reasoning, .plan, .toolResult, .complete,
        ]
        return order.compactMap { stage in
            ws.debug.thoughtsByStage[stage].map { (stage, $0) }
        }
    }

    @ViewBuilder
    private func section<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
            Divider()
        }
    }

    private func row(_ label: String, _ value: String, mono: Bool = false) -> some View {
        HStack(alignment: .top) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Spacer()
            Text(value)
                .font(mono ? .caption.monospaced() : .caption)
                .textSelection(.enabled)
                .multilineTextAlignment(.trailing)
        }
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}
