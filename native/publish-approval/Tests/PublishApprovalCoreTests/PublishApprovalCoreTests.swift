import CryptoKit
import Darwin
import Foundation
import Testing
@testable import PublishApprovalCore

private let fixedNow = Date(timeIntervalSince1970: 1_788_939_320)

@Test func canonicalJSONMatchesPythonStyleUnicodeEncoding() throws {
    let value = JSONValue.object([
        "z": .string("貓/測試"),
        "a": .object(["newline": .string("第一行\n第二行")]),
        "items": .array([.integer(2), .bool(true), .null]),
    ])
    #expect(
        String(data: try CanonicalJSON.encode(value), encoding: .utf8)
            == #"{"a":{"newline":"第一行\n第二行"},"items":[2,true,null],"z":"貓/測試"}"#
    )
}

@Test func strictParserRejectsDuplicateKeysAndPreservesFiniteNumbers() throws {
    #expect(throws: StrictJSONError.self) {
        var parser = try StrictJSONParser(data: Data(#"{"a":1,"a":2}"#.utf8))
        _ = try parser.parse()
    }
    var parser = try StrictJSONParser(data: Data(#"{"a":1.25,"b":1e-06}"#.utf8))
    let parsed = try parser.parse()
    #expect(try CanonicalJSON.encode(parsed) == Data(#"{"a":1.25,"b":1e-06}"#.utf8))
}

@Test func securityVerifierAcceptsCryptoKitP256DERSignature() throws {
    let privateKey = P256.Signing.PrivateKey()
    let message = Data("canonical approval statement".utf8)
    let signature = try privateKey.signature(for: message).derRepresentation
    #expect(try SignatureVerification.verifyP256(
        message: message,
        signature: signature,
        publicKeyX963: privateKey.publicKey.x963Representation
    ))
    #expect(try !SignatureVerification.verifyP256(
        message: Data("tampered approval statement".utf8),
        signature: signature,
        publicKeyX963: privateKey.publicKey.x963Representation
    ))
}

@Test func secureEnclaveFailureOnlyReportsErrorDomainAndCode() throws {
    let sensitive = "refresh-token-must-not-appear"
    let error = CFErrorCreate(
        kCFAllocatorDefault,
        "org.videostudio.test" as CFString,
        42,
        ["sensitive": sensitive] as CFDictionary
    )
    let message = SecureEnclaveApprovalKey.safeErrorIdentity(error)
    #expect(message == "domain=org.videostudio.test code=42")
    #expect(!message.contains(sensitive))
}

@Test func verifiedRequestUsesActualMetadataForPromptAndCanonicalStatement() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request(title: "系統訊號解說", description: "這是實際檔案裡的說明。")
    let verified = try ApprovalRequestVerifier(now: { fixedNow }).verify(request)
    #expect(verified.reviewText.contains("標題：系統訊號解說"))
    #expect(verified.reviewText.contains("說明：這是實際檔案裡的說明。"))
    #expect(verified.reviewText.contains("請求的頻道：\(fixture.channelID)"))
    #expect(verified.reviewText.contains("最終服務仍會比對安裝時固定的頻道政策"))
    #expect(verified.reviewText.contains("可見度：不公開（unlisted）"))
    #expect(verified.reviewText.contains("Render self-eval SHA-256:"))
    #expect(verified.reviewText.contains("Visual QA review SHA-256:"))
    #expect(verified.requestedChannelID == fixture.channelID)

    var parser = try StrictJSONParser(data: request)
    let object = try #require(parser.parse().objectValue)
    let attestation = try #require(object["attestation"])
    let expected = try CanonicalJSON.encode(.object([
        "schema": .string(ApprovalContract.statementSchema),
        "action": .string(ApprovalContract.action),
        "attestation": attestation,
    ]))
    #expect(verified.statement == expected)
}

@Test func verifierRejectsMalformedOrMismatchedRequestedChannel() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let malformed = try fixture.request(
        intentChannelID: "not-a-channel",
        attestationChannelID: "not-a-channel"
    )
    #expect(throws: ApprovalError.self) {
        _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(malformed)
    }

    let mismatched = try fixture.request(
        intentChannelID: "UCbbbbbbbbbbbbbbbbbbbbbb",
        attestationChannelID: fixture.channelID
    )
    #expect(throws: ApprovalError.self) {
        _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(mismatched)
    }
}

@Test func verifierRejectsTamperedCanonicalFileBeforeSigning() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request()
    try Data("changed video".utf8).write(to: fixture.project.appendingPathComponent("output/final.mp4"))
    #expect(throws: ApprovalError.self) {
        _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(request)
    }
}

@Test func verifierRejectsTraversalInBoundEvidenceReference() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request(selfEvalPath: "../outside.json")
    #expect(throws: ApprovalError.self) {
        _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(request)
    }
}

@Test func verifierRejectsMismatchedExpiredFutureAndOverlongRequests() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let cases = try [
        fixture.request(attestationProjectID: "another-project"),
        fixture.request(issuedAt: "2026-09-09T07:20:00+00:00", expiresAt: "2026-09-09T07:25:00+00:00"),
        fixture.request(issuedAt: "2026-09-09T07:36:00+00:00", expiresAt: "2026-09-09T07:37:00+00:00"),
        fixture.request(issuedAt: "2026-09-09T07:30:00+00:00", expiresAt: "2026-09-09T07:35:01+00:00"),
    ]
    for request in cases {
        #expect(throws: ApprovalError.self) {
            _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(request)
        }
    }
}

@Test func verifierRejectsProjectRootDigestMismatch() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request(projectRootSHA: String(repeating: "f", count: 64))
    #expect(throws: ApprovalError.self) {
        _ = try ApprovalRequestVerifier(now: { fixedNow }).verify(request)
    }
}

@Test func issuerUsesFakeKeyAndCreatesLeafOnce() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request()
    let storeRoot = fixture.root.appendingPathComponent("attestations")
    let key = FakeKey(pin: fixture.pin, signature: Data([0x30, 0x01, 0x00]))
    let issuer = ApprovalIssuer(key: key, store: ApprovalLeafStore(root: storeRoot), now: { fixedNow })
    let reference = try issuer.issue(requestData: request)
    #expect(reference == fixture.reference)
    #expect(key.signCount == 1)
    #expect(key.lastReason?.contains("intent") == true)

    let leafURL = storeRoot.appendingPathComponent(String(reference.dropFirst("attestation:".count)) + ".json")
    var parser = try StrictJSONParser(data: Data(contentsOf: leafURL))
    let leaf = try #require(parser.parse().objectValue)
    #expect(leaf["schema"] == .string(ApprovalContract.attestationSchema))
    #expect(leaf["signature_base64"] == .string(key.signature.base64EncodedString()))
    #expect(leaf["consumed_at"] == .null)
    #expect(leaf["consumed_project_id"] == .null)
    #expect(leaf["consumed_intent_sha256"] == .null)

    var status = stat()
    #expect(lstat(storeRoot.path, &status) == 0)
    #expect(status.st_mode & 0o777 == 0o700)
    #expect(lstat(leafURL.path, &status) == 0)
    #expect(status.st_mode & 0o777 == 0o600)
    #expect(throws: ApprovalError.self) { _ = try issuer.issue(requestData: request) }
}

@Test func issuerDropsSignatureIfRequestExpiresDuringPrompt() throws {
    let fixture = try Fixture()
    defer { fixture.remove() }
    let request = try fixture.request(expiresAt: "2026-09-09T07:35:30+00:00")
    let values = LockedDates([fixedNow, fixedNow, fixedNow.addingTimeInterval(31)])
    let key = FakeKey(pin: fixture.pin, signature: Data([0x30, 0x00]))
    let storeRoot = fixture.root.appendingPathComponent("attestations")
    let issuer = ApprovalIssuer(key: key, store: ApprovalLeafStore(root: storeRoot), now: { values.next() })
    #expect(throws: ApprovalError.self) { _ = try issuer.issue(requestData: request) }
    #expect(key.signCount == 1)
    #expect(!FileManager.default.fileExists(atPath: storeRoot.path))
}

private final class FakeKey: ApprovalSigningKey {
    let pin: PublicKeyPin
    let signature: Data
    var signCount = 0
    var lastReason: String?

    init(pin: PublicKeyPin, signature: Data) {
        self.pin = pin
        self.signature = signature
    }
    func enroll() throws -> PublicKeyPin { pin }
    func keyInfo() throws -> PublicKeyPin { pin }
    func sign(_ message: Data, reason: String, expectedKeyID: String) throws -> Data {
        #expect(expectedKeyID == pin.keyID)
        #expect(!message.isEmpty)
        signCount += 1
        lastReason = reason
        return signature
    }
}

private final class LockedDates: @unchecked Sendable {
    private var values: [Date]
    private let lock = NSLock()
    init(_ values: [Date]) { self.values = values }
    func next() -> Date {
        lock.lock()
        defer { lock.unlock() }
        if values.count == 1 { return values[0] }
        return values.removeFirst()
    }
}

private final class Fixture {
    let root: URL
    let project: URL
    let projectID = "fixture"
    let channelID = "UCaaaaaaaaaaaaaaaaaaaaaa"
    let reference = "attestation:01234567-89ab-4def-8123-456789abcdef"
    let nonce = String(repeating: "a", count: 64)
    let publicKey = Data([0x04] + [UInt8](repeating: 0x11, count: 64))
    let pin: PublicKeyPin

    init() throws {
        let temporary = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("video-studio-approval-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: false)
        root = temporary
        project = root.appendingPathComponent(projectID, isDirectory: true)
        try FileManager.default.createDirectory(
            at: project.appendingPathComponent("output", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: project.appendingPathComponent("quality-review/render-self-eval", isDirectory: true),
            withIntermediateDirectories: true
        )
        try FileManager.default.createDirectory(
            at: project.appendingPathComponent("quality-review/visual-sampling", isDirectory: true),
            withIntermediateDirectories: true
        )
        pin = PublicKeyPin(
            algorithm: ApprovalContract.algorithm,
            keyID: ApprovalRequestVerifier.sha256Hex(publicKey),
            publicKeyX963: publicKey
        )
    }

    func remove() {
        try? FileManager.default.removeItem(at: root)
    }

    func request(
        title: String = "Video Studio",
        description: String = "A real description",
        selfEvalPath: String = "projects/fixture/quality-review/render-self-eval/result.json",
        attestationProjectID: String? = nil,
        intentChannelID: String? = nil,
        attestationChannelID: String? = nil,
        issuedAt: String = "2026-09-09T07:35:00+00:00",
        expiresAt: String = "2026-09-09T07:40:00+00:00",
        projectRootSHA: String? = nil
    ) throws -> Data {
        let final = Data("video bytes".utf8)
        let cover = Data("png bytes".utf8)
        let selfEval = Data(#"{"ok":true}"#.utf8)
        let visualReview = Data(#"{"verdict":"pass"}"#.utf8)
        let metadata = try CanonicalJSON.encode(.object([
            "schema": .string("haru.publish_metadata.v1"),
            "project": .string(projectID),
            "title": .string(title),
            "description": .string(description),
        ]))
        try final.write(to: project.appendingPathComponent("output/final.mp4"))
        try cover.write(to: project.appendingPathComponent("output/cover.png"))
        try metadata.write(to: project.appendingPathComponent("publish-metadata.json"))
        try selfEval.write(to: project.appendingPathComponent("quality-review/render-self-eval/result.json"))
        try visualReview.write(to: project.appendingPathComponent("quality-review/visual-sampling/review.json"))

        let intent = JSONValue.object([
            "schema": .string(ApprovalContract.intentSchema),
            "project_id": .string(projectID),
            "final_sha256": .string(ApprovalRequestVerifier.sha256Hex(final)),
            "final_bytes": .integer(Int64(final.count)),
            "metadata_sha256": .string(ApprovalRequestVerifier.sha256Hex(metadata)),
            "cover_sha256": .string(ApprovalRequestVerifier.sha256Hex(cover)),
            "channel_id": .string(intentChannelID ?? channelID),
            "visibility": .string(ApprovalContract.visibility),
            "warnings_acknowledged": .array([]),
            "override_reason": .null,
            "runtime_contract": .object([
                "schema": .string("haru.project_runtime_contract.v1"),
                "runtime": .string("haru.runtime.v1"),
                "evaluator": .string("haru.evaluator.v1"),
                "artifact": .string("haru.artifact.v1"),
            ]),
            "render_self_eval": .object([
                "path": .string(selfEvalPath),
                "sha256": .string(ApprovalRequestVerifier.sha256Hex(selfEval)),
                "bytes": .integer(Int64(selfEval.count)),
            ]),
            "visual_qa_review": .object([
                "path": .string("projects/fixture/quality-review/visual-sampling/review.json"),
                "sha256": .string(ApprovalRequestVerifier.sha256Hex(visualReview)),
                "bytes": .integer(Int64(visualReview.count)),
            ]),
            "generation": .integer(1),
            "attestation_ref": .string(reference),
            "nonce": .string(nonce),
        ])
        let digest = ApprovalRequestVerifier.sha256Hex(try CanonicalJSON.encode(intent))
        let attestation = JSONValue.object([
            "schema": .string(ApprovalContract.attestationSchema),
            "attestation_ref": .string(reference),
            "project_id": .string(attestationProjectID ?? projectID),
            "approval_intent_sha256": .string(digest),
            "nonce": .string(nonce),
            "generation": .integer(1),
            "issued_at": .string(issuedAt),
            "expires_at": .string(expiresAt),
            "channel_id": .string(attestationChannelID ?? channelID),
            "visibility": .string(ApprovalContract.visibility),
            "key_id": .string(pin.keyID),
            "signature_algorithm": .string(ApprovalContract.algorithm),
            "project_root_sha256": .string(
                projectRootSHA ?? ApprovalRequestVerifier.sha256Hex(Data(project.path.utf8))
            ),
        ])
        return try CanonicalJSON.encode(.object([
            "schema": .string(ApprovalContract.requestSchema),
            "project_root": .string(project.path),
            "intent": intent,
            "intent_sha256": .string(digest),
            "attestation": attestation,
        ]))
    }
}
