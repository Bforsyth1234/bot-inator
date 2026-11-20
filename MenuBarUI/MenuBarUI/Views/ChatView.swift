import Combine
import SwiftUI

/// The Chat page's state. #142: owned by `AppDelegate` for the app's
/// lifetime (not by the view), so a turn keeps collecting its thoughts and
/// reply while the Chat page is off screen, and the draft survives too.
/// The bookkeeping itself is ``ChatTranscript`` (Services/ChatTranscript.swift);
/// this class feeds it:
///   1. live `thought` frames from `WebSocketManager.messages`, each once,
///      by index (append-only, #132),
///   2. `GET /api/chats` rows: when the page appears, after each reconnect,
///      shortly after a turn's `complete` (the full reply; the thought is cut
///      at 500 characters), and every ``pendingPollInterval`` while a turn is
///      pending (catches a `complete` frame lost to a dropped socket or a
///      daemon restart).
@MainActor
final class ChatViewModel: ObservableObject {
    @Published private(set) var turns: [ChatTurn] = []
    @Published var draft: String = ""
    @Published var errorMessage: String?

    /// While a turn is pending: how often history is re-read and stale
    /// turns are expired.
    static let pendingPollInterval: TimeInterval = 15
    /// After a `complete`: when to re-read history for the logged reply
    /// (the daemon writes the assistant row a moment after the thought).
    static let replyRefetchDelays: [TimeInterval] = [1, 4]

    private var transcript = ChatTranscript()
    private let baseURL: URL
    private let session: URLSession
    private var scanned = 0
    private var cancellables: Set<AnyCancellable> = []
    private var fetchInFlight = false
    private var fetchAgain = false
    private var pollTask: Task<Void, Never>?
    /// #143: where Stop sends `chat_cancel` (set by ``attach(to:)``).
    private weak var ws: WebSocketManager?

    /// #143: a turn is still running; the send button is Stop.
    var hasPending: Bool { turns.contains { $0.isPending } }

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Follow the socket for the app's lifetime. Called once at launch.
    func attach(to ws: WebSocketManager) {
        guard cancellables.isEmpty else { return }
        self.ws = ws
        ws.$messages
            .receive(on: RunLoop.main)
            .sink { [weak self] messages in
                MainActor.assumeIsolated { self?.ingest(messages: messages) }
            }
            .store(in: &cancellables)
        // A (re)connect may have missed frames: re-read what was logged.
        ws.$isConnected
            .removeDuplicates()
            .filter { $0 }
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    Task { await self.refreshHistory() }
                }
            }
            .store(in: &cancellables)
    }

    /// Re-read the persisted transcript and merge it (idempotent). Called
    /// when the Chat page appears and by the triggers above; concurrent
    /// calls collapse into one more fetch.
    func refreshHistory() async {
        if fetchInFlight { fetchAgain = true; return }
        fetchInFlight = true
        defer { fetchInFlight = false }
        repeat {
            fetchAgain = false
            do {
                let request = try DaemonAuth.request(baseURL.appendingPathComponent("api/chats"))
                let (data, response) = try await session.data(for: request)
                try DaemonAuth.checkAuth(response)
                let rows = try JSONDecoder().decode([ChatHistoryRow].self, from: data)
                transcript.merge(history: rows, now: Date())
                errorMessage = nil
                publish()
            } catch {
                // Keep what is shown; the next trigger tries again.
                errorMessage = "Failed to load history: \(error.localizedDescription)"
            }
        } while fetchAgain
    }

    /// Fold new frames in. Only frames past ``scanned`` are read: each
    /// frame is ingested once, whatever the page is doing.
    func ingest(messages: [WSMessage]) {
        if messages.count < scanned { scanned = 0 }
        guard scanned < messages.count else { return }
        var completed: [String] = []
        var sawThought = false
        for index in scanned..<messages.count {
            guard case let .thought(_, timestamp, payload) = messages[index] else { continue }
            sawThought = true
            if transcript.ingest(payload, key: index, at: timestamp) {
                completed.append(payload.eventId)
            }
        }
        scanned = messages.count
        if sawThought { publish() }
        if !completed.isEmpty { refetchForReply(completed) }
    }

    /// Record a locally-typed user message under the `message_id` that
    /// ``WebSocketManager/sendUserMessage(_:context:)`` returned.
    func submit(text: String, messageId: String) {
        transcript.submit(id: messageId, text: text, at: Date())
        publish()
    }

    /// End a turn from the app side without waiting for the daemon.
    func finish(id: String, text: String) {
        transcript.finish(id: id, text: text)
        publish()
    }

    /// #143: Stop. Every pending turn shows "Stopped." at once and the
    /// daemon is told (`chat_cancel`); its `complete` and stopped history row
    /// then confirm it. A second press finds nothing pending: no-op.
    func stop() {
        let ids = transcript.stopAll(at: Date())
        guard !ids.isEmpty else { return }
        for id in ids { ws?.sendChatCancel(id) }
        publish()
        // The daemon's stopped row (what happened, e.g. a withdrawn card).
        refetchForReply(ids)
    }

    // MARK: - Private

    private func publish() {
        let next = transcript.turns
        if next != turns { turns = next }
        updatePolling()
    }

    /// Re-read history until each just-completed turn has its logged row.
    private func refetchForReply(_ ids: [String]) {
        Task { [weak self] in
            for delay in Self.replyRefetchDelays {
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
                guard let self else { return }
                await self.refreshHistory()
                let logged = ids.allSatisfy { self.transcript.byId[$0]?.replyFromHistory ?? true }
                if logged { return }
            }
        }
    }

    /// Poll only while something is pending.
    private func updatePolling() {
        if transcript.hasPending {
            guard pollTask == nil else { return }
            let interval = Self.pendingPollInterval
            pollTask = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
                    guard !Task.isCancelled, let self else { return }
                    if self.transcript.expireStale(now: Date()) { self.publish() }
                    await self.refreshHistory()
                    if !self.transcript.hasPending { break }
                }
                // A cancelled loop was already replaced (or cleared) by
                // whoever cancelled it.
                if !Task.isCancelled { self?.pollTask = nil }
            }
        } else {
            pollTask?.cancel()
            pollTask = nil
        }
    }
}

// MARK: - View

struct ChatView: View {
    @EnvironmentObject private var ws: WebSocketManager
    /// #142: app-lifetime state (``AppDelegate/chat``), not view state.
    @EnvironmentObject private var viewModel: ChatViewModel
    @FocusState private var inputFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            transcript
            Divider()
            composer
        }
        .frame(minWidth: 480, minHeight: 520)
        .task {
            inputFocused = true
            // Frames are ingested app-wide; this picks up anything only
            // chat_log has (a reply logged while the socket was down).
            await viewModel.refreshHistory()
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .foregroundStyle(Color.accentColor)
                .appFont(.title3)
            VStack(alignment: .leading, spacing: 1) {
                Text("Chat").appFont(.headline)
                Text(ws.isConnected ? "Connected" : "Offline")
                    .appFont(.secondary)
                    .foregroundStyle(ws.isConnected ? Color.secondary : Color.red)
                if !ws.isConnected, let hint = ws.authHint {
                    Text(hint)
                        .appFont(.fine)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer()
        }
        .padding(12)
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if let error = viewModel.errorMessage {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                            .appFont(.secondary)
                    }
                    ForEach(viewModel.turns) { turn in
                        TurnRow(turn: turn).id(turn.id)
                    }
                }
                .padding(12)
            }
            .onChange(of: viewModel.turns.last?.thoughts.count) { _, _ in
                if let last = viewModel.turns.last {
                    withAnimation(.easeOut(duration: 0.15)) {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
            .onChange(of: viewModel.turns.count) { _, _ in
                if let last = viewModel.turns.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
            .onChange(of: viewModel.turns.last?.assistantText) { _, _ in
                if let last = viewModel.turns.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
            .onAppear {
                // #142: the turns outlive the page; open at the latest one.
                if let last = viewModel.turns.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }

    private var composer: some View {
        HStack(spacing: 8) {
            TextField("Ask the agent…", text: $viewModel.draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...5)
                .focused($inputFocused)
                .onSubmit(send)
            if viewModel.hasPending {
                // #143: while a turn runs the button is Stop (and Esc stops).
                Button(action: viewModel.stop) {
                    Image(systemName: "stop.fill")
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                .keyboardShortcut(.escape, modifiers: [])
                .help("Stop (Esc)")
            } else {
                Button(action: send) {
                    Image(systemName: "paperplane.fill")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.return, modifiers: [])
                .disabled(viewModel.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(12)
    }

    private func send() {
        let text = viewModel.draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let messageId = ws.sendUserMessage(text)
        viewModel.submit(text: text, messageId: messageId)
        viewModel.draft = ""
    }
}

// MARK: - Turn rendering

private struct TurnRow: View {
    let turn: ChatTurn
    @State private var reasoningExpanded: Bool = false
    @Environment(\.textSize) private var textSize

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !turn.userText.isEmpty {
                HStack {
                    Spacer(minLength: 40)
                    Text(turn.userText)
                        .textSelection(.enabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.accentColor.opacity(0.85), in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(.white)
                }
            }
            agentBubble
        }
    }

    @ViewBuilder
    private var agentBubble: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "brain.head.profile")
                .foregroundStyle(.secondary)
                .frame(width: 18)
                .padding(.top, 4)
            VStack(alignment: .leading, spacing: 4) {
                if !turn.assistantText.isEmpty && turn.stopped {
                    // #143: a stopped turn reads as a status line, not an answer.
                    Label(turn.assistantText, systemImage: "stop.circle")
                        .textSelection(.enabled)
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                } else if !turn.assistantText.isEmpty {
                    Text(turn.assistantText)
                        .textSelection(.enabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                } else if turn.isPending {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("thinking…")
                            .appFont(.secondary)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                } else if turn.noReply {
                    Text("No reply was recorded for this message.")
                        .appFont(.secondary)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                }
                reasoningDisclosure
            }
            Spacer(minLength: 40)
        }
    }

    @ViewBuilder
    private var reasoningDisclosure: some View {
        let inner = turn.thoughts.filter { $0.payload.stage != .complete }
        if !inner.isEmpty {
            DisclosureGroup(isExpanded: $reasoningExpanded) {
                VStack(alignment: .leading, spacing: 4) {
                    // Keyed by frame index (unique, #132), not by content.
                    ForEach(inner) { thought in
                        let t = thought.payload
                        HStack(alignment: .top, spacing: 6) {
                            Text(t.stage.rawValue)
                                .appFont(.fine, weight: .semibold)
                                .foregroundStyle(.secondary)
                                .frame(width: 104 * textSize.layoutScale, alignment: .leading)
                            Text(t.content)
                                .appFont(.secondary)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.top, 4)
            } label: {
                Text("Reasoning (\(inner.count))")
                    .appFont(.secondary)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
