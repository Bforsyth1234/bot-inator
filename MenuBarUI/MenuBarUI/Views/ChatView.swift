import SwiftUI

/// A single turn in the chat — either a historical pair loaded from
/// `/api/chats` or a live turn assembled from the user's submission
/// plus the `thought` frames streamed back over the WebSocket.
struct ChatTurn: Identifiable, Hashable {
    let id: String  // event_id shared by user message + all agent thoughts
    var userText: String
    var assistantText: String
    var thoughts: [ThoughtPayload]
    var isPending: Bool  // true until a `complete` thought arrives
}

/// Derives the ordered list of `ChatTurn`s from:
///   1. Prior transcript fetched from `GET /api/chats`
///   2. The local `WebSocketManager.messages` stream (live thoughts)
/// The former provides historical continuity across daemon restarts;
/// the latter provides the live reasoning stream for the current turn.
@MainActor
final class ChatViewModel: ObservableObject {
    @Published private(set) var turns: [ChatTurn] = []
    @Published var draft: String = ""
    @Published var errorMessage: String?

    private var orderedIds: [String] = []
    private var byId: [String: ChatTurn] = [:]
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!,
         session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// Load the persisted transcript from the daemon. Called once when
    /// the chat window appears.
    func loadHistory() async {
        do {
            let url = baseURL.appendingPathComponent("api/chats")
            let (data, _) = try await session.data(from: url)
            let rows = try JSONDecoder().decode([ChatLogRow].self, from: data)
            for row in rows { upsert(row: row) }
            errorMessage = nil
        } catch {
            errorMessage = "Failed to load history: \(error.localizedDescription)"
        }
    }

    /// Fold any new thoughts from the WS stream into the matching turn.
    /// Safe to call on every `messages` change — upserts are idempotent.
    func ingest(messages: [WSMessage]) {
        for msg in messages {
            guard case let .thought(_, _, payload) = msg else { continue }
            appendThought(payload)
        }
    }

    /// Record a locally-typed user message. Returns the generated
    /// `event_id` (== `message_id`) so the caller can forward it to
    /// ``WebSocketManager.sendUserMessage``.
    func submit(text: String, messageId: String) {
        var turn = byId[messageId] ?? ChatTurn(
            id: messageId, userText: "", assistantText: "",
            thoughts: [], isPending: true
        )
        turn.userText = text
        turn.isPending = true
        byId[messageId] = turn
        if !orderedIds.contains(messageId) { orderedIds.append(messageId) }
        rebuild()
    }

    // MARK: - Private

    private func appendThought(_ payload: ThoughtPayload) {
        var turn = byId[payload.eventId] ?? ChatTurn(
            id: payload.eventId, userText: "", assistantText: "",
            thoughts: [], isPending: true
        )
        // Ignore duplicates (e.g. a reconnect re-delivers the same frame).
        if !turn.thoughts.contains(payload) {
            turn.thoughts.append(payload)
        }
        if payload.stage == .complete {
            turn.assistantText = payload.content
            turn.isPending = false
        }
        byId[payload.eventId] = turn
        if !orderedIds.contains(payload.eventId) {
            orderedIds.append(payload.eventId)
        }
        rebuild()
    }

    private func upsert(row: ChatLogRow) {
        var turn = byId[row.eventId] ?? ChatTurn(
            id: row.eventId, userText: "", assistantText: "",
            thoughts: [], isPending: false
        )
        switch row.role {
        case "user": turn.userText = row.text
        case "assistant": turn.assistantText = row.text
        default: break
        }
        byId[row.eventId] = turn
        if !orderedIds.contains(row.eventId) { orderedIds.append(row.eventId) }
        rebuild()
    }

    private func rebuild() {
        turns = orderedIds.compactMap { byId[$0] }
    }
}

/// Row returned by `GET /api/chats`. Only the fields the UI needs.
private struct ChatLogRow: Codable {
    let eventId: String
    let role: String
    let text: String

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case role
        case text
    }
}

// MARK: - View

struct ChatView: View {
    @EnvironmentObject private var ws: WebSocketManager
    @StateObject private var viewModel = ChatViewModel()
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
            await viewModel.loadHistory()
            viewModel.ingest(messages: ws.messages)
            inputFocused = true
        }
        .onChange(of: ws.messages.count) { _, _ in
            viewModel.ingest(messages: ws.messages)
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "bubble.left.and.bubble.right.fill")
                .foregroundStyle(Color.accentColor)
                .font(.title3)
            VStack(alignment: .leading, spacing: 1) {
                Text("Chat").font(.headline)
                Text(ws.isConnected ? "Connected" : "Offline")
                    .font(.caption)
                    .foregroundStyle(ws.isConnected ? Color.secondary : Color.red)
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
                            .font(.caption)
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
        }
    }

    private var composer: some View {
        HStack(spacing: 8) {
            TextField("Ask the agent…", text: $viewModel.draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...5)
                .focused($inputFocused)
                .onSubmit(send)
            Button(action: send) {
                Image(systemName: "paperplane.fill")
            }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.return, modifiers: [])
            .disabled(viewModel.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
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
                if !turn.assistantText.isEmpty {
                    Text(turn.assistantText)
                        .textSelection(.enabled)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
                } else if turn.isPending {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("thinking…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
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
        let inner = turn.thoughts.filter { $0.stage != .complete }
        if !inner.isEmpty {
            DisclosureGroup(isExpanded: $reasoningExpanded) {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(inner, id: \.self) { t in
                        HStack(alignment: .top, spacing: 6) {
                            Text(t.stage.rawValue)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.secondary)
                                .frame(width: 90, alignment: .leading)
                            Text(t.content)
                                .font(.caption)
                                .textSelection(.enabled)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(.top, 4)
            } label: {
                Text("Reasoning (\(inner.count))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
