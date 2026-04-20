import Foundation
import Combine

@MainActor
final class WebSocketManager: ObservableObject {
    @Published private(set) var messages: [WSMessage] = []
    @Published private(set) var isConnected: Bool = false
    @Published private(set) var pendingApproval: (seq: Int, payload: ApprovalRequestPayload)?
    @Published private(set) var pendingCodeApproval: (seq: Int, payload: CodeApprovalRequestPayload)?
    @Published private(set) var lastStatus: StatusPayload?
    @Published private(set) var debug: DebugStats = DebugStats()

    private let url: URL
    private let session: URLSession
    private var task: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?

    private var outgoingSeq: Int = 1_000_000
    private var reconnectDelay: TimeInterval = 1.0
    private let maxReconnectDelay: TimeInterval = 30.0

    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    init(url: URL = URL(string: "ws://localhost:8000/ws/stream")!,
         session: URLSession = .shared) {
        self.url = url
        self.session = session

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { dec in
            let raw = try dec.singleValueContainer().decode(String.self)
            if let d = ISO8601.parse(raw) { return d }
            throw DecodingError.dataCorruptedError(
                in: try dec.singleValueContainer(),
                debugDescription: "Invalid ISO8601 date: \(raw)"
            )
        }
        self.decoder = decoder

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, enc in
            var c = enc.singleValueContainer()
            try c.encode(ISO8601.format(date))
        }
        self.encoder = encoder
    }

    private enum ISO8601 {
        static func parse(_ raw: String) -> Date? {
            let full = ISO8601DateFormatter()
            full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let d = full.date(from: raw) { return d }
            let basic = ISO8601DateFormatter()
            basic.formatOptions = [.withInternetDateTime]
            return basic.date(from: raw)
        }

        static func format(_ date: Date) -> String {
            let f = ISO8601DateFormatter()
            f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return f.string(from: date)
        }
    }

    // MARK: - Lifecycle

    func connect() {
        guard task == nil else { return }
        let task = session.webSocketTask(with: url)
        self.task = task
        task.resume()
        isConnected = true
        reconnectDelay = 1.0
        debug.connectAttempts += 1
        receiveTask = Task { [weak self] in await self?.receiveLoop() }
    }

    func resetDebugStats() {
        debug = DebugStats()
    }

    func disconnect() {
        receiveTask?.cancel()
        reconnectTask?.cancel()
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        receiveTask = nil
        reconnectTask = nil
        isConnected = false
    }

    // MARK: - Send

    func sendApprovalResponse(
        requestId: String,
        approved: Bool,
        userNote: String?,
        editedArgs: JSONValue? = nil
    ) {
        let payload = ApprovalResponsePayload(
            requestId: requestId,
            approved: approved,
            userNote: (userNote?.isEmpty == true) ? nil : userNote,
            editedArgs: editedArgs
        )
        let msg = WSMessage.approvalResponse(
            seq: nextSeq(),
            timestamp: Date(),
            payload: payload
        )
        send(msg)
        if pendingApproval?.payload.requestId == requestId {
            pendingApproval = nil
        }
    }

    func sendCodeApprovalResponse(
        requestId: String,
        approved: Bool,
        editedCode: String? = nil,
        userNote: String? = nil
    ) {
        let payload = CodeApprovalResponsePayload(
            requestId: requestId,
            approved: approved,
            editedCode: editedCode,
            userNote: (userNote?.isEmpty == true) ? nil : userNote
        )
        let msg = WSMessage.codeApprovalResponse(
            seq: nextSeq(),
            timestamp: Date(),
            payload: payload
        )
        send(msg)
        if pendingCodeApproval?.payload.requestId == requestId {
            pendingCodeApproval = nil
        }
    }

    func sendCommand(_ action: CommandAction) {
        let msg = WSMessage.command(
            seq: nextSeq(),
            timestamp: Date(),
            payload: CommandPayload(action: action)
        )
        send(msg)
    }

    /// Send a user chat turn. Returns the generated `messageId` so the
    /// caller can immediately render a local user bubble keyed by the
    /// same id the daemon will echo back on every `thought` frame.
    @discardableResult
    func sendUserMessage(_ text: String) -> String {
        let messageId = "msg_" + UUID().uuidString
            .replacingOccurrences(of: "-", with: "")
            .prefix(12).lowercased()
        let msg = WSMessage.userMessage(
            seq: nextSeq(),
            timestamp: Date(),
            payload: UserMessagePayload(messageId: messageId, text: text)
        )
        send(msg)
        return messageId
    }

    private func send(_ message: WSMessage) {
        guard let task else { return }
        do {
            let data = try encoder.encode(message)
            guard let string = String(data: data, encoding: .utf8) else { return }
            task.send(.string(string)) { error in
                if let error { NSLog("WebSocketManager send error: \(error)") }
            }
        } catch {
            NSLog("WebSocketManager encode error: \(error)")
        }
    }

    private func nextSeq() -> Int {
        outgoingSeq += 1
        return outgoingSeq
    }

    // MARK: - Receive

    private func receiveLoop() async {
        guard let task else { return }
        while !Task.isCancelled {
            do {
                let message = try await task.receive()
                await handleIncoming(message)
            } catch {
                NSLog("WebSocketManager receive error: \(error)")
                debug.lastConnectError = String(describing: error)
                await scheduleReconnect()
                return
            }
        }
    }

    private func handleIncoming(_ message: URLSessionWebSocketTask.Message) async {
        let data: Data?
        let rawPreview: String?
        switch message {
        case .string(let s):
            data = s.data(using: .utf8)
            rawPreview = String(s.prefix(240))
        case .data(let d):
            data = d
            rawPreview = String(data: d.prefix(240), encoding: .utf8)
        @unknown default:
            data = nil
            rawPreview = nil
        }
        guard let data else { return }
        debug.framesReceived += 1
        if let rawPreview { debug.lastRawFrame = rawPreview }
        do {
            let parsed = try decoder.decode(WSMessage.self, from: data)
            debug.framesDecoded += 1
            debug.recordDecoded(parsed)
            messages.append(parsed)
            switch parsed {
            case .approvalRequest(let seq, _, let payload):
                pendingApproval = (seq, payload)
            case .codeApprovalRequest(let seq, _, let payload):
                pendingCodeApproval = (seq, payload)
            case .status(_, _, let payload):
                lastStatus = payload
            default:
                break
            }
        } catch {
            NSLog("WebSocketManager decode error: \(error)")
            debug.recordDecodeError(error, raw: rawPreview)
        }
    }

    // MARK: - Reconnect

    private func scheduleReconnect() async {
        isConnected = false
        task?.cancel(with: .abnormalClosure, reason: nil)
        task = nil
        debug.reconnectsScheduled += 1
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, maxReconnectDelay)
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.connect() }
        }
    }
}

// MARK: - Debug

/// Diagnostic counters surfaced in the Debug tab. All mutations happen on
/// the main actor via `WebSocketManager`.
struct DebugStats {
    var framesReceived: Int = 0
    var framesDecoded: Int = 0
    var decodeErrorCount: Int = 0
    var connectAttempts: Int = 0
    var reconnectsScheduled: Int = 0

    var framesByType: [String: Int] = [:]
    var thoughtsByStage: [ThoughtStage: Int] = [:]

    var lastDecodeError: String?
    var lastConnectError: String?
    var lastRawFrame: String?
    var recentDecodeErrors: [DecodeErrorEntry] = []

    struct DecodeErrorEntry: Identifiable, Hashable {
        let id = UUID()
        let timestamp: Date
        let message: String
        let rawPreview: String?
    }

    private static let maxErrorHistory = 10

    mutating func recordDecoded(_ message: WSMessage) {
        let key: String
        switch message {
        case .thought(_, _, let payload):
            key = "thought"
            thoughtsByStage[payload.stage, default: 0] += 1
        case .approvalRequest: key = "approval_request"
        case .approvalResponse: key = "approval_response"
        case .codeApprovalRequest: key = "code_approval_request"
        case .codeApprovalResponse: key = "code_approval_response"
        case .status: key = "status"
        case .command: key = "command"
        case .userMessage: key = "user_message"
        }
        framesByType[key, default: 0] += 1
    }

    mutating func recordDecodeError(_ error: Error, raw: String?) {
        decodeErrorCount += 1
        let msg = String(describing: error)
        lastDecodeError = msg
        let entry = DecodeErrorEntry(timestamp: Date(), message: msg, rawPreview: raw)
        recentDecodeErrors.append(entry)
        if recentDecodeErrors.count > Self.maxErrorHistory {
            recentDecodeErrors.removeFirst(recentDecodeErrors.count - Self.maxErrorHistory)
        }
    }
}
