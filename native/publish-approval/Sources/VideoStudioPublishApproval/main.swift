import Darwin
import AppKit
import Foundation
import PublishApprovalCore

@main @MainActor
struct VideoStudioPublishApprovalCommand {
    static func main() {
        do {
            try run(Array(CommandLine.arguments.dropFirst()))
        } catch {
            writeError("video-studio-publish-approve: \(error)\n")
            Darwin.exit(1)
        }
    }

    private static func run(_ arguments: [String]) throws {
        let key = SecureEnclaveApprovalKey()
        if arguments.count == 2, arguments[0] == "sign" {
            let request = try DirectFile.read(path: arguments[1])
            let issuer = ApprovalIssuer(key: key, store: ApprovalLeafStore())
            let verified = try issuer.verify(request)
            guard presentReview(verified.reviewText) else {
                throw ApprovalError.invalidRequest("the approval review was cancelled")
            }
            let reference = try issuer.issue(verifiedRequest: verified)
            FileHandle.standardOutput.write(Data((reference + "\n").utf8))
            return
        }
        if arguments.count == 2, arguments[0] == "self-eval-sign" {
            let request = try DirectFile.read(path: arguments[1])
            let issuer = SelfEvalApprovalIssuer(key: key, store: SelfEvalLeafStore())
            let verified = try issuer.verify(request)
            guard presentSelfEvalReview(verified.reviewText) else {
                throw ApprovalError.invalidRequest("the self-eval operator review was cancelled")
            }
            let reference = try issuer.issue(verifiedRequest: verified)
            FileHandle.standardOutput.write(Data((reference + "\n").utf8))
            return
        }
        if arguments.count == 2, arguments[0] == "self-eval-verify" {
            let request = try DirectFile.read(path: arguments[1])
            let verified = try SelfEvalApprovalRequestVerifier().verify(request)
            try writeJSON(.object([
                "ok": .bool(true),
                "attestation_ref": .string(verified.attestationReference),
                "key_id": .string(verified.expectedKeyID),
            ]))
            return
        }
        switch arguments {
        case ["enroll"]:
            try writeJSON(key.enroll().json)
        case ["key-info"]:
            try writeJSON(key.keyInfo().json)
        case ["self-test"]:
            try writeJSON(ApprovalIssuer(key: key, store: ApprovalLeafStore()).selfTest())
        default:
            throw ApprovalError.invalidRequest(
                "usage: video-studio-publish-approve enroll | key-info | self-test | sign /absolute/path/request.json | self-eval-verify /absolute/path/request.json | self-eval-sign /absolute/path/request.json"
            )
        }
    }

    private static func writeJSON(_ value: JSONValue) throws {
        var data = try CanonicalJSON.encode(value)
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    }

    private static func writeError(_ message: String) {
        FileHandle.standardError.write(Data(message.utf8))
    }

    private static func presentReview(_ text: String) -> Bool {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        application.activate(ignoringOtherApps: true)

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 660, height: 430))
        textView.string = text
        textView.isEditable = false
        textView.isSelectable = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textContainerInset = NSSize(width: 10, height: 10)
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true

        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 660, height: 430))
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .bezelBorder
        scrollView.documentView = textView

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "檢查 Video Studio 發布核准內容"
        alert.informativeText = "請逐項確認下方內容。繼續後，macOS 會要求本人驗證並簽章；這個動作本身不會上傳影片。"
        alert.accessoryView = scrollView
        alert.addButton(withTitle: "核准並簽章")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }

    private static func presentSelfEvalReview(_ text: String) -> Bool {
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        application.activate(ignoringOtherApps: true)

        let textView = NSTextView(frame: NSRect(x: 0, y: 0, width: 660, height: 430))
        textView.string = text
        textView.isEditable = false
        textView.isSelectable = true
        textView.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        textView.textContainerInset = NSSize(width: 10, height: 10)
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true

        let scrollView = NSScrollView(frame: NSRect(x: 0, y: 0, width: 660, height: 430))
        scrollView.hasVerticalScroller = true
        scrollView.hasHorizontalScroller = false
        scrollView.borderType = .bezelBorder
        scrollView.documentView = textView

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "檢查 Video Studio self-eval 操作者聲明"
        alert.informativeText = "請確認這是實際的 vision provider 未設定聲明或人工 fallback 結果。繼續後 macOS 會要求本人驗證並簽章；這個動作不會發布影片。"
        alert.accessoryView = scrollView
        alert.addButton(withTitle: "確認並簽章")
        alert.addButton(withTitle: "取消")
        return alert.runModal() == .alertFirstButtonReturn
    }
}
