import Foundation
import Combine

@MainActor
final class WebSocketManager: ObservableObject {
    @Published private(set) var messages: [WSMessage] = []
    @Published private(set) var isConnected: Bool = false
    @Published private(set) var pendingApproval: (seq: Int, payload: ApprovalRequestPayload)?
    @Published private(set) var lastStatus: StatusPayload?

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
        receiveTask = Task { [weak self] in await self?.receiveLoop() }
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

    func sendApprovalResponse(requestId: String, approved: Bool, userNote: String?) {
        let payload = ApprovalResponsePayload(
            requestId: requestId,
            approved: approved,
            userNote: (userNote?.isEmpty == true) ? nil : userNote
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

    func sendCommand(_ action: CommandAction) {
        let msg = WSMessage.command(
            seq: nextSeq(),
            timestamp: Date(),
            payload: CommandPayload(action: action)
        )
        send(msg)
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
                await scheduleReconnect()
                return
            }
        }
    }

    private func handleIncoming(_ message: URLSessionWebSocketTask.Message) async {
        let data: Data?
        switch message {
        case .string(let s): data = s.data(using: .utf8)
        case .data(let d): data = d
        @unknown default: data = nil
        }
        guard let data else { return }
        do {
            let parsed = try decoder.decode(WSMessage.self, from: data)
            messages.append(parsed)
            switch parsed {
            case .approvalRequest(let seq, _, let payload):
                pendingApproval = (seq, payload)
            case .status(_, _, let payload):
                lastStatus = payload
            default:
                break
            }
        } catch {
            NSLog("WebSocketManager decode error: \(error)")
        }
    }

    // MARK: - Reconnect

    private func scheduleReconnect() async {
        isConnected = false
        task?.cancel(with: .abnormalClosure, reason: nil)
        task = nil
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, maxReconnectDelay)
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await MainActor.run { self?.connect() }
        }
    }
}
