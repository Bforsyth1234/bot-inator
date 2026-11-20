import Foundation

// #142: the Chat page's turn bookkeeping, kept free of SwiftUI so it can be
// exercised outside the app. ``ChatViewModel`` (Views/ChatView.swift) owns
// one of these for the app's lifetime and feeds it live `thought` frames and
// `GET /api/chats` rows; the view only renders ``ChatTranscript/turns``.

/// One reasoning step of a chat turn. `key` is the frame's index in
/// `WebSocketManager.messages`, which is append-only and so unique for the
/// app's lifetime (#132: `seq` repeats across daemon restarts, and two steps
/// may legitimately carry the same text).
struct ChatThought: Identifiable, Hashable {
    let key: Int
    let payload: ThoughtPayload
    var id: Int { key }
}

/// One row of `GET /api/chats` (`{id, event_id, role, text, created_at}`).
struct ChatHistoryRow: Decodable, Hashable {
    let eventId: String
    let role: String
    let text: String
    /// SQLite `CURRENT_TIMESTAMP` ("YYYY-MM-DD HH:MM:SS", UTC). Nil if absent
    /// or unparseable.
    let createdAt: Date?

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case role, text
        case createdAt = "created_at"
    }

    init(eventId: String, role: String, text: String, createdAt: Date?) {
        self.eventId = eventId
        self.role = role
        self.text = text
        self.createdAt = createdAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        eventId = try c.decode(String.self, forKey: .eventId)
        role = try c.decode(String.self, forKey: .role)
        text = try c.decode(String.self, forKey: .text)
        let raw = try? c.decodeIfPresent(String.self, forKey: .createdAt)
        createdAt = raw.flatMap(Self.parseTimestamp)
    }

    static func parseTimestamp(_ raw: String) -> Date? {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        for format in ["yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss"] {
            f.dateFormat = format
            if let d = f.date(from: raw) { return d }
        }
        return nil
    }
}

/// A single exchange, keyed by the user message's id (== the daemon's
/// event id, carried by every `thought` of the turn and both `chat_log` rows).
struct ChatTurn: Identifiable, Hashable {
    let id: String
    var userText: String
    var assistantText: String
    var thoughts: [ChatThought]
    /// The daemon is still working on this turn: set by ``ChatTranscript/submit``
    /// or by a recent history turn with a user row and no assistant row.
    /// Cleared by its `complete` thought, its assistant row, a stop (#143:
    /// ``ChatTranscript/finish(id:text:)``) or going stale.
    var isPending: Bool
    /// When the user asked (submit time, or the user row's `created_at`).
    /// Only orders the transcript.
    var startedAt: Date?
    /// When the turn last showed signs of life (submit, user row, a thought).
    /// Drives the staleness rule.
    var lastActivity: Date?
    /// Given up on: no reply ``ChatTranscript/staleAfter`` after the last
    /// sign of life (an abandoned turn, e.g. the daemon restarted mid-turn).
    /// A reply that turns up later still fills the turn in.
    var noReply: Bool
    /// `assistantText` is the full `chat_log` row, not the `complete`
    /// thought (which the daemon cuts at 500 characters).
    var replyFromHistory: Bool
    /// #143: the turn ended by Stop. Set at once by ``ChatTranscript/stop(id:at:)``
    /// (optimistic), then follows the daemon's text: its reply for a stopped
    /// turn starts with ``ChatTranscript/stoppedText``; a real answer that
    /// beat the Stop clears it.
    var stopped: Bool

    init(id: String, userText: String = "", assistantText: String = "",
         thoughts: [ChatThought] = [], isPending: Bool = false,
         startedAt: Date? = nil, lastActivity: Date? = nil, noReply: Bool = false,
         replyFromHistory: Bool = false, stopped: Bool = false) {
        self.id = id
        self.userText = userText
        self.assistantText = assistantText
        self.thoughts = thoughts
        self.isPending = isPending
        self.startedAt = startedAt
        self.lastActivity = lastActivity
        self.noReply = noReply
        self.replyFromHistory = replyFromHistory
        self.stopped = stopped
    }
}

/// The ordered transcript. Pure value type: every input is explicit
/// (frames with their index, history rows, `now`).
struct ChatTranscript {
    /// A pending turn with no thought for this long is treated as abandoned:
    /// the spinner stops and "No reply was recorded" shows. Approval cards
    /// time out after 120 s (code cards 300 s), so a live turn is never idle
    /// this long.
    static let staleAfter: TimeInterval = 10 * 60
    /// Thoughts for event ids that are not (yet) a turn are kept briefly, so
    /// a turn that history introduces later (e.g. a Quick Ask turn, or a
    /// frame that beat the history fetch) still gets its reasoning and reply.
    static let maxOrphanEvents = 64
    /// #143: what the daemon's reply for a stopped turn starts with
    /// (`ai/chat_turns.py` `STOPPED_TEXT`), and what Stop shows at once.
    static let stoppedText = "Stopped."

    static func isStoppedReply(_ text: String) -> Bool {
        text.hasPrefix(stoppedText)
    }
    static let maxOrphanThoughtsPerEvent = 64

    private(set) var order: [String] = []
    private(set) var byId: [String: ChatTurn] = [:]
    private var orphans: [String: [(thought: ChatThought, at: Date)]] = [:]
    private var orphanOrder: [String] = []

    var turns: [ChatTurn] { order.compactMap { byId[$0] } }
    var pendingIds: [String] { order.filter { byId[$0]?.isPending == true } }
    var hasPending: Bool { byId.values.contains { $0.isPending } }

    /// A message the user just sent from this app.
    mutating func submit(id: String, text: String, at now: Date) {
        var turn = byId[id] ?? ChatTurn(id: id, startedAt: now)
        turn.userText = text
        turn.isPending = true
        turn.noReply = false
        turn.lastActivity = now
        insert(turn, at: now)
        adoptOrphans(for: id)
    }

    /// Fold one live `thought` frame in. `key` is its index in
    /// `WebSocketManager.messages`; a frame seen before is ignored, so
    /// re-feeding the same messages never duplicates a step.
    /// Returns true when the frame is a `complete` for a known turn (the
    /// caller then re-reads history for the full reply text).
    @discardableResult
    mutating func ingest(_ payload: ThoughtPayload, key: Int, at time: Date) -> Bool {
        let thought = ChatThought(key: key, payload: payload)
        guard var turn = byId[payload.eventId] else {
            keepOrphan(thought, at: time)
            return false
        }
        guard !turn.thoughts.contains(where: { $0.key == key }) else { return false }
        apply(thought, at: time, to: &turn)
        byId[turn.id] = turn
        return payload.stage == .complete
    }

    /// Merge `GET /api/chats` rows (oldest first). Idempotent.
    mutating func merge(history rows: [ChatHistoryRow], now: Date) {
        var answered: Set<String> = []
        var asked: [String: Date?] = [:]
        var created: Set<String> = []
        for row in rows {
            if byId[row.eventId] == nil {
                insert(ChatTurn(id: row.eventId, startedAt: row.createdAt,
                                lastActivity: row.createdAt),
                       at: row.createdAt)
                created.insert(row.eventId)
            }
            guard var turn = byId[row.eventId] else { continue }
            switch row.role {
            case "user":
                if turn.userText.isEmpty { turn.userText = row.text }
                asked.updateValue(row.createdAt, forKey: row.eventId)
            case "assistant":
                answered.insert(row.eventId)
                turn.assistantText = row.text
                turn.replyFromHistory = true
                turn.isPending = false
                turn.noReply = false
                turn.stopped = Self.isStoppedReply(row.text)
            default:
                break
            }
            byId[row.eventId] = turn
        }
        // A user row with no assistant row is a turn still running, unless
        // it is old (abandoned). Only decided for turns history introduced:
        // a turn sent from here is already pending, and a finished one stays
        // finished.
        for (id, createdAt) in asked where created.contains(id) && !answered.contains(id) {
            guard var turn = byId[id], turn.assistantText.isEmpty else { continue }
            let since = createdAt.map { now.timeIntervalSince($0) } ?? .infinity
            turn.isPending = since < Self.staleAfter
            turn.noReply = !turn.isPending
            byId[id] = turn
        }
        for id in created { adoptOrphans(for: id) }
        _ = expireStale(now: now)
    }

    /// Stop spinning for turns idle longer than ``staleAfter``.
    /// Returns true when a turn changed.
    @discardableResult
    mutating func expireStale(now: Date) -> Bool {
        var changed = false
        for id in pendingIds {
            guard var turn = byId[id], let last = turn.lastActivity,
                  now.timeIntervalSince(last) >= Self.staleAfter else { continue }
            turn.isPending = false
            turn.noReply = turn.assistantText.isEmpty
            byId[id] = turn
            changed = true
        }
        return changed
    }

    /// End a turn from the app side: clear the pending flag and show `text`
    /// without waiting for the daemon; the daemon's own `complete` /
    /// assistant row, when they come, overwrite it as usual.
    mutating func finish(id: String, text: String) {
        guard var turn = byId[id] else { return }
        turn.isPending = false
        turn.noReply = false
        if turn.assistantText.isEmpty { turn.assistantText = text }
        byId[id] = turn
    }

    /// #143: the user pressed Stop on turn `id`. Optimistic: the spinner
    /// stops and "Stopped." shows now; the daemon's `complete` and its
    /// stopped `chat_log` row confirm it (and fill in what happened). If a
    /// real answer beat the Stop to the daemon, that answer arrives the same
    /// way and replaces "Stopped." (the daemon's log is what history shows).
    /// Returns true when the turn was pending (send `chat_cancel` then);
    /// false for an unknown or already-ended turn, so a second Stop is a no-op.
    @discardableResult
    mutating func stop(id: String, at now: Date) -> Bool {
        guard var turn = byId[id], turn.isPending else { return false }
        turn.isPending = false
        turn.noReply = false
        turn.stopped = true
        turn.lastActivity = now
        if turn.assistantText.isEmpty { turn.assistantText = Self.stoppedText }
        byId[id] = turn
        return true
    }

    /// #143: Stop every pending turn; the ids to send `chat_cancel` for.
    mutating func stopAll(at now: Date) -> [String] {
        pendingIds.filter { stop(id: $0, at: now) }
    }

    // MARK: - Private

    private mutating func apply(_ thought: ChatThought, at time: Date, to turn: inout ChatTurn) {
        turn.thoughts.append(thought)
        turn.thoughts.sort { $0.key < $1.key }
        turn.lastActivity = max(turn.lastActivity ?? time, time)
        if thought.payload.stage == .complete {
            // Never replace the full chat_log reply with the cut-down thought.
            if !turn.replyFromHistory || turn.assistantText.isEmpty {
                turn.assistantText = thought.payload.content
                turn.stopped = Self.isStoppedReply(thought.payload.content)
            }
            turn.isPending = false
            turn.noReply = false
        }
    }

    /// Insert keeping the order by start time; a turn with no time goes last.
    private mutating func insert(_ turn: ChatTurn, at time: Date?) {
        let isNew = byId[turn.id] == nil
        byId[turn.id] = turn
        guard isNew else { return }
        guard let time else { order.append(turn.id); return }
        let index = order.firstIndex { other in
            guard let t = byId[other]?.startedAt else { return false }
            return t > time
        }
        if let index { order.insert(turn.id, at: index) } else { order.append(turn.id) }
    }

    private mutating func keepOrphan(_ thought: ChatThought, at time: Date) {
        let id = thought.payload.eventId
        if orphans[id] == nil {
            orphanOrder.append(id)
            if orphanOrder.count > Self.maxOrphanEvents {
                orphans[orphanOrder.removeFirst()] = nil
            }
        }
        var list = orphans[id, default: []]
        guard !list.contains(where: { $0.thought.key == thought.key }) else { return }
        list.append((thought, time))
        if list.count > Self.maxOrphanThoughtsPerEvent { list.removeFirst() }
        orphans[id] = list
    }

    private mutating func adoptOrphans(for id: String) {
        guard let list = orphans.removeValue(forKey: id), var turn = byId[id] else { return }
        orphanOrder.removeAll { $0 == id }
        for (thought, at) in list where !turn.thoughts.contains(where: { $0.key == thought.key }) {
            apply(thought, at: at, to: &turn)
        }
        byId[id] = turn
    }
}
