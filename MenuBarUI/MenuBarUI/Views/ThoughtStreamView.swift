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
        .frame(width: 420, height: 520)
        .onAppear { ws.connect() }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(ws.isConnected ? Color.green : Color.red)
                .frame(width: 8, height: 8)
            Text(ws.isConnected ? "Connected" : "Disconnected")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            if let state = ws.lastStatus?.state {
                Text(state.rawValue)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    // MARK: - Thought list

    private var thoughtList: some View {
        ScrollViewReader { proxy in
            List(thoughts, id: \.seq) { entry in
                ThoughtRow(seq: entry.seq, timestamp: entry.timestamp, payload: entry.payload)
                    .id(entry.seq)
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
            }
            .listStyle(.plain)
            .onAppear {
                scrollToBottom(proxy: proxy, animated: false)
            }
            .onChange(of: thoughts.count) { _, _ in
                scrollToBottom(proxy: proxy, animated: true)
            }
        }
    }

    private func scrollToBottom(proxy: ScrollViewProxy, animated: Bool) {
        guard let last = thoughts.last else { return }
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: 0.15)) {
                    proxy.scrollTo(last.seq, anchor: .bottom)
                }
            } else {
                proxy.scrollTo(last.seq, anchor: .bottom)
            }
        }
    }

    private var thoughts: [(seq: Int, timestamp: Date, payload: ThoughtPayload)] {
        ws.messages.compactMap { msg in
            if case let .thought(seq, ts, payload) = msg {
                return (seq, ts, payload)
            }
            return nil
        }
    }

    // MARK: - Footer

    private var footer: some View {
        HStack {
            Button {
                ws.sendCommand(.pauseListeners)
            } label: { Label("Pause", systemImage: "pause.circle") }
            Button {
                ws.sendCommand(.resumeListeners)
            } label: { Label("Resume", systemImage: "play.circle") }
            Spacer()
            Button {
                NSApplication.shared.terminate(nil)
            } label: { Label("Quit", systemImage: "power") }
        }
        .buttonStyle(.borderless)
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
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
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                StageBadge(stage: payload.stage)
                Spacer()
            }
            Text(payload.content)
                .font(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct StageBadge: View {
    let stage: ThoughtStage

    var body: some View {
        Text(stage.rawValue)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }

    private var color: Color {
        switch stage {
        case .eventReceived: return .blue
        case .analysis: return .indigo
        case .reasoning: return .purple
        case .plan: return .orange
        case .toolResult: return .teal
        case .complete: return .green
        }
    }
}
