import SwiftUI

/// #130: the small chat thread on the tool-idea and code-review cards, in
/// place of the old "Note (optional)" field. Talks to
/// `POST /api/card-chat/{request_id}`; the daemon keeps the thread in memory
/// only and drops it when the card closes. Nothing typed here approves or
/// denies anything: only the card's buttons decide.
@MainActor
final class CardChatModel: ObservableObject {
    struct Turn: Identifiable, Hashable {
        let id: Int
        let role: String
        let text: String
        var isUser: Bool { role == "user" }
    }

    struct Plan: Hashable {
        let description: String
        let expectedLogic: String
    }

    @Published private(set) var turns: [Turn] = []
    @Published var input: String = ""
    @Published private(set) var isSending = false
    /// The code card asked for a change: a new card is on its way and this
    /// one can no longer be approved.
    @Published private(set) var rewriting = false
    /// The idea card's plan as changed in chat (nil = unchanged).
    @Published private(set) var plan: Plan?
    @Published private(set) var errorMessage: String?
    @Published private(set) var turnsLeft: Int?
    /// Set when the card closed under the chat (410).
    @Published private(set) var closed = false

    var requestId: String = ""
    private let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:8000")!, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// True while Approve must stay off: a reply could still change the card.
    var blocksApproval: Bool { isSending || rewriting }

    var canSend: Bool {
        !isSending && !rewriting && !closed && !requestId.isEmpty
            && !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && (turnsLeft ?? 1) > 0
    }

    func send() async {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canSend, !text.isEmpty else { return }
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let path = "/api/card-chat/" + (requestId.addingPercentEncoding(
                withAllowedCharacters: .urlPathAllowed) ?? requestId)
            guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
                throw URLError(.badURL)
            }
            var request = try DaemonAuth.request(url, method: "POST")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
            request.timeoutInterval = 150
            let (data, response) = try await session.data(for: request)
            try DaemonAuth.checkAuth(response)
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                if http.statusCode == 410 { closed = true }
                errorMessage = (json["detail"] as? String) ?? "HTTP \(http.statusCode)"
                return
            }
            input = ""
            apply(json)
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func apply(_ json: [String: Any]) {
        if let raw = json["turns"] as? [[String: Any]] {
            turns = raw.enumerated().map { index, item in
                Turn(id: index, role: item["role"] as? String ?? "assistant",
                     text: item["text"] as? String ?? "")
            }
        }
        if let p = json["plan"] as? [String: Any],
           let description = p["description"] as? String,
           let logic = p["expected_logic"] as? String {
            plan = Plan(description: description, expectedLogic: logic)
        }
        rewriting = (json["rewriting"] as? Bool) ?? rewriting
        turnsLeft = json["turns_left"] as? Int
    }
}

/// The thread, its input and the "chat can't approve" line.
struct CardChatView: View {
    @ObservedObject var model: CardChatModel
    let title: String
    let placeholder: String
    /// Called when the user sends (e.g. to stop a countdown).
    var onActivity: (() -> Void)? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                    .appFont(.secondary, weight: .semibold)
                    .foregroundStyle(.secondary)
                Spacer()
                if model.isSending {
                    ProgressView().controlSize(.small)
                }
            }
            if !model.turns.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(model.turns) { turn in
                                bubble(turn).id(turn.id)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(6)
                    }
                    .frame(height: 150)
                    .background(Color.secondary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6))
                    .onChange(of: model.turns.count) { _, _ in
                        if let last = model.turns.last { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }
            HStack(spacing: 6) {
                TextField(placeholder, text: $model.input, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...3)
                    .disabled(model.rewriting || model.closed)
                    .onSubmit(sendNow)
                Button("Send", action: sendNow)
                    .disabled(!model.canSend)
            }
            if let error = model.errorMessage {
                Text(error)
                    .appFont(.fine)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Chat can't approve or deny. Only the buttons below decide.")
                .appFont(.fine)
                .foregroundStyle(.secondary)
        }
    }

    private func sendNow() {
        onActivity?()
        Task { await model.send() }
    }

    private func bubble(_ turn: CardChatModel.Turn) -> some View {
        HStack {
            if turn.isUser { Spacer(minLength: 40) }
            Text(turn.text)
                .appFont(.body)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(turn.isUser ? Color.accentColor.opacity(0.18) : Color.secondary.opacity(0.12),
                            in: RoundedRectangle(cornerRadius: 8))
            if !turn.isUser { Spacer(minLength: 40) }
        }
    }
}
