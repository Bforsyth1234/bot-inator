import SwiftUI

struct ThoughtStreamView: View {
    @EnvironmentObject private var ws: WebSocketManager

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            thoughtList
            Divider()
            footer
        }
        .frame(minWidth: 380, maxWidth: .infinity, minHeight: 300, maxHeight: .infinity)
        .onAppear { ws.connect() }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(ws.isConnected ? Color.green : Color.red)
                .frame(width: 8, height: 8)
            Text(ws.isConnected ? "Connected" : "Disconnected")
                .appFont(.secondary)
                .foregroundStyle(.secondary)
            Spacer()
            if let state = ws.lastStatus?.state {
                Text(state.rawValue)
                    .appFont(.secondary, design: .monospaced)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Thought list

    /// `thoughts` scans and sorts every message, so it is built once per
    /// redraw here and passed down, not recomputed for each use.
    private var thoughtList: some View {
        let thoughts = self.thoughts
        return ScrollViewReader { proxy in
            List(thoughts) { entry in
                ThoughtRow(seq: entry.seq, timestamp: entry.timestamp, payload: entry.payload)
                    .id(entry.id)
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
            }
            .listStyle(.plain)
            .onAppear {
                scrollToBottom(thoughts.last, proxy: proxy, animated: false)
            }
            .onChange(of: thoughts.count) { _, _ in
                scrollToBottom(thoughts.last, proxy: proxy, animated: true)
            }
        }
    }

    private func scrollToBottom(_ last: ThoughtEntry?, proxy: ScrollViewProxy, animated: Bool) {
        guard let last else { return }
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            } else {
                proxy.scrollTo(last.id, anchor: .bottom)
            }
        }
    }

    /// #132: rows are keyed by their position in `ws.messages`, not by the
    /// daemon's `seq`. `seq` restarts whenever the daemon does while the app
    /// keeps its messages, so two thoughts could share a seq and SwiftUI
    /// would misplace rows. `ws.messages` is append-only (QuickAskPanel
    /// relies on that too), so the index is unique and stable. Shown in
    /// timestamp order; ties keep arrival order.
    private var thoughts: [ThoughtEntry] {
        ThoughtEntry.ordered(from: ws.messages)
    }

    // MARK: - Footer

    private var pausedText: String {
        guard let until = ws.lastStatus?.pausedUntil, until != "indefinite",
              let date = ISO8601DateFormatter.delivery.date(from: until) else {
            return "Paused"
        }
        return "Paused until \(date.formatted(date: .omitted, time: .shortened))"
    }

    private var footer: some View {
        HStack {
            // #87: a real Pause (listeners keep running); the status frame
            // says whether it is on.
            if ws.lastStatus?.paused == true {
                Button {
                    ws.sendCommand(.resumeListeners)
                } label: { Label("Resume", systemImage: "play.circle") }
                Text(pausedText)
                    .foregroundStyle(.secondary)
            } else {
                Menu {
                    ForEach(PauseDuration.allCases) { duration in
                        Button(duration.label) {
                            ws.sendCommand(.pauseListeners, duration: duration)
                        }
                    }
                } label: { Label("Pause", systemImage: "pause.circle") }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }
            Spacer()
            Button {
                NSApplication.shared.terminate(nil)
            } label: { Label("Quit", systemImage: "power") }
        }
        .buttonStyle(.borderless)
        .appFont(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }
}

struct ThoughtEntry: Identifiable {
    /// Index in `WebSocketManager.messages`: unique across daemon restarts.
    let id: Int
    let seq: Int
    let timestamp: Date
    let payload: ThoughtPayload

    /// Thoughts in `messages`, sorted by timestamp; equal timestamps keep
    /// arrival order (the index breaks the tie).
    static func ordered(from messages: [WSMessage]) -> [ThoughtEntry] {
        var entries: [ThoughtEntry] = []
        for (index, msg) in messages.enumerated() {
            if case let .thought(seq, ts, payload) = msg {
                entries.append(ThoughtEntry(id: index, seq: seq, timestamp: ts, payload: payload))
            }
        }
        entries.sort { lhs, rhs in
            lhs.timestamp != rhs.timestamp ? lhs.timestamp < rhs.timestamp : lhs.id < rhs.id
        }
        return entries
    }
}

private struct ThoughtRow: View {
    let seq: Int
    let timestamp: Date
    let payload: ThoughtPayload

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(Self.timeFormatter.string(from: timestamp))
                    .appFont(.fine, design: .monospaced)
                    .foregroundStyle(.secondary)
                StageBadge(stage: payload.stage)
                Spacer()
            }
            Text(payload.content)
                .appFont(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct StageBadge: View {
    let stage: ThoughtStage

    var body: some View {
        Text(stage.rawValue)
            .appFont(.fine, weight: .semibold)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }

    private var color: Color {
        switch stage {
        case .eventReceived: return .blue
        case .analysis: return .indigo
        case .memory: return .pink
        case .reasoning: return .purple
        case .plan: return .orange
        case .toolResult: return .teal
        case .complete: return .green
        }
    }
}
