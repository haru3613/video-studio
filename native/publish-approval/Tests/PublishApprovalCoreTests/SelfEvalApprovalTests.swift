import Darwin
import Foundation
import Testing
@testable import PublishApprovalCore

private let selfEvalFixedNow = Date(timeIntervalSince1970: 1_788_939_320)

@Test func verifiesOperatorUnavailableRequestAgainstCurrentEvidence() throws {
    let fixture = try SelfEvalRequestFixture()
    defer { fixture.remove() }
    let request = try fixture.request(action: SelfEvalApprovalContract.unavailableAction)
    let verified = try SelfEvalApprovalRequestVerifier(now: { selfEvalFixedNow }).verify(request)
    #expect(verified.reviewText.contains("This runtime has no configured vision provider"))
    #expect(verified.reviewText.contains("Verdict: unavailable"))
    #expect(verified.authenticationReason.contains("不是模型審查結果"))
}

@Test func refusesOperatorVisionPassAndChangedCurrentResult() throws {
    let fixture = try SelfEvalRequestFixture()
    defer { fixture.remove() }
    let forbidden = try fixture.request(
        action: SelfEvalApprovalContract.unavailableAction,
        verdict: "pass"
    )
    #expect(throws: ApprovalError.self) {
        _ = try SelfEvalApprovalRequestVerifier(now: { selfEvalFixedNow }).verify(forbidden)
    }

    let request = try fixture.request(action: SelfEvalApprovalContract.unavailableAction)
    try Data("changed".utf8).write(to: fixture.project.appendingPathComponent(SelfEvalApprovalContract.resultPath))
    #expect(throws: ApprovalError.self) {
        _ = try SelfEvalApprovalRequestVerifier(now: { selfEvalFixedNow }).verify(request)
    }
}

@Test func verifiesHumanFailWithFractionalFindingAfterSignedV2Unavailability() throws {
    let fixture = try SelfEvalRequestFixture()
    defer { fixture.remove() }
    let request = try fixture.request(
        action: SelfEvalApprovalContract.humanReviewAction,
        verdict: "fail",
        findings: [
            .object([
                "timestamp_seconds": .number("1.25"),
                "boundary_id": .string("boundary-1"),
                "category": .string("visual_discontinuity"),
                "severity": .string("fail"),
                "message": .string("Visible discontinuity at the cut."),
            ]),
        ]
    )
    let verified = try SelfEvalApprovalRequestVerifier(now: { selfEvalFixedNow }).verify(request)
    #expect(verified.reviewText.contains("Verdict: fail"))
    #expect(verified.reviewText.contains("Findings (1):"))
    #expect(verified.reviewText.contains("\"timestamp_seconds\":1.25"))
}

@Test func selfEvalIssuerSignsOnceAndWritesNonOverwritingLeaf() throws {
    let fixture = try SelfEvalRequestFixture()
    defer { fixture.remove() }
    let request = try fixture.request(action: SelfEvalApprovalContract.unavailableAction)
    let verified = try SelfEvalApprovalRequestVerifier(now: { selfEvalFixedNow }).verify(request)
    let storeRoot = fixture.root.appendingPathComponent("self-eval-attestations")
    let key = SelfEvalFakeKey(pin: fixture.pin, signature: Data([0x30, 0x01, 0x00]))
    let issuer = SelfEvalApprovalIssuer(
        key: key,
        store: SelfEvalLeafStore(root: storeRoot),
        now: { selfEvalFixedNow }
    )
    #expect(try issuer.issue(verifiedRequest: verified) == fixture.reference)
    #expect(key.signCount == 1)
    #expect(key.lastReason?.contains("不會發布影片") == true)
    let leaf = storeRoot.appendingPathComponent(String(fixture.reference.dropFirst("self-eval-attestation:".count)) + ".json")
    var parser = try StrictJSONParser(data: Data(contentsOf: leaf))
    let value = try #require(parser.parse().objectValue)
    #expect(value["schema"] == .string(SelfEvalApprovalContract.visionAttestationSchema))
    #expect(value["signature_base64"] == .string(key.signature.base64EncodedString()))
    #expect(value["consumed_at"] == .null)
    var status = stat()
    #expect(lstat(storeRoot.path, &status) == 0)
    #expect(status.st_mode & 0o777 == 0o700)
    #expect(lstat(leaf.path, &status) == 0)
    #expect(status.st_mode & 0o777 == 0o600)
    #expect(throws: ApprovalError.self) { _ = try issuer.issue(verifiedRequest: verified) }
}

@Test func signedStatementHasPythonCanonicalFloatSpellings() throws {
    let value = JSONValue.object([
        "a": .number("1.25"),
        "b": .number("1.0"),
        "c": .number("1e-06"),
    ])
    #expect(
        String(data: try CanonicalJSON.encode(value), encoding: .utf8)
            == #"{"a":1.25,"b":1.0,"c":1e-06}"#
    )
}

@Test func signedStatementMatchesPythonGolden() throws {
    let immutable = JSONValue.object([
        "schema": .string(SelfEvalApprovalContract.visionAttestationSchema),
        "action": .string(SelfEvalApprovalContract.unavailableAction),
        "generation": .integer(1),
    ])
    let statement = JSONValue.object([
        "schema": .string(SelfEvalApprovalContract.statementSchema),
        "action": .string(SelfEvalApprovalContract.unavailableAction),
        "attestation": immutable,
    ])
    #expect(
        String(data: try CanonicalJSON.encode(statement), encoding: .utf8)
            == #"{"action":"declare_vision_unavailable","attestation":{"action":"declare_vision_unavailable","generation":1,"schema":"haru.self_eval_vision_attestation.v2"},"schema":"haru.self_eval_authorization_statement.v1"}"#
    )
}

private final class SelfEvalFakeKey: ApprovalSigningKey {
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

private final class SelfEvalRequestFixture {
    let root: URL
    let project: URL
    let projectID = "fixture"
    let reference = "self-eval-attestation:01234567-89ab-4def-8123-456789abcdef"
    let identity = String(repeating: "1", count: 64)
    let publicKey = Data([0x04] + [UInt8](repeating: 0x22, count: 64))
    let pin: PublicKeyPin

    init() throws {
        root = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
            .appendingPathComponent("video-studio-self-eval-approval-tests-\(UUID().uuidString)", isDirectory: true)
        project = root.appendingPathComponent(projectID, isDirectory: true)
        try FileManager.default.createDirectory(
            at: project.appendingPathComponent("quality-review/render-self-eval/attempts/attempt-01", isDirectory: true),
            withIntermediateDirectories: true
        )
        pin = PublicKeyPin(
            algorithm: SelfEvalApprovalContract.algorithm,
            keyID: SelfEvalApprovalRequestVerifier.sha256Hex(publicKey),
            publicKeyX963: publicKey
        )
    }

    func remove() {
        try? FileManager.default.removeItem(at: root)
    }

    func request(
        action: String,
        verdict: String? = nil,
        findings: [JSONValue] = []
    ) throws -> Data {
        let evaluationPath = "quality-review/render-self-eval/attempts/attempt-01/evaluation.json"
        let indexPath = "quality-review/render-self-eval/attempts/attempt-01/evidence-index.json"
        let evaluationData = Data(#"{"fixture":"evaluation"}"#.utf8)
        let indexData = Data(#"{"fixture":"index"}"#.utf8)
        try evaluationData.write(to: project.appendingPathComponent(evaluationPath))
        try indexData.write(to: project.appendingPathComponent(indexPath))
        let evaluation = referenceValue(path: evaluationPath, data: evaluationData)
        let evidenceIndex = referenceValue(path: indexPath, data: indexData)
        let result = JSONValue.object([
            "schema": .string("haru.render_self_eval.v1"),
            "project": .string(projectID),
            "status": .string("needs_human"),
            "attempt": .integer(1),
            "max_attempts": .integer(3),
            "attempt_identity": .string(identity),
            "inputs": .array([]),
            "boundary_policy": .null,
            "boundary_plan": .null,
            "evaluation": evaluation,
            "evidence_index": evidenceIndex,
            "review": .null,
            "outcome": .null,
            "tool": .object([:]),
            "findings": .array([]),
            "verdict": .string("needs_human"),
            "remediation": .object([:]),
            "next_action": .string("await_reviewer_verdict"),
            "updated_at": .string("2026-09-09T07:35:00+00:00"),
        ])
        let resultData = try CanonicalJSON.encode(result)
        try resultData.write(to: project.appendingPathComponent(SelfEvalApprovalContract.resultPath))
        let resultReference = referenceValue(path: SelfEvalApprovalContract.resultPath, data: resultData)

        let isUnavailable = action == SelfEvalApprovalContract.unavailableAction
        let actualVerdict = verdict ?? (isUnavailable ? "unavailable" : "pass")
        let reviewInput = JSONValue.object([
            "reviewer_kind": .string(isUnavailable ? "vision" : "human_fallback"),
            "verdict": .string(actualVerdict),
            "reviewed_by": .string("Harvey"),
            "provider": .string(isUnavailable ? SelfEvalApprovalContract.configurationProvider : "human-operator"),
            "model": .string(isUnavailable ? SelfEvalApprovalContract.configurationModel : "none"),
            "capability": .string(isUnavailable ? SelfEvalApprovalContract.configurationCapability : SelfEvalApprovalContract.humanCapability),
            "notes": .string(isUnavailable ? "This runtime has no configured vision provider." : "Reviewed the complete current evidence."),
            "findings": .array(findings),
        ])
        var intent = try #require(reviewInput.objectValue)
        intent["schema"] = .string(
            isUnavailable ? SelfEvalApprovalContract.visionIntentSchema : SelfEvalApprovalContract.humanIntentSchema
        )
        intent["project_id"] = .string(projectID)
        intent["attempt"] = .integer(1)
        intent["attempt_identity"] = .string(identity)
        intent["evaluation"] = evaluation
        intent["evidence_index"] = evidenceIndex
        if !isUnavailable {
            let unavailablePath = "quality-review/render-self-eval/attempts/attempt-01/vision-unavailable.json"
            let unavailable = JSONValue.object([
                "schema": .string("haru.render_self_eval_vision_unavailable.v1"),
                "project": .string(projectID),
                "attempt": .integer(1),
                "attempt_identity": .string(identity),
                "evaluation": evaluation,
                "evidence_index": evidenceIndex,
                "reviewed_by": .string("Harvey"),
                "provider": .string(SelfEvalApprovalContract.configurationProvider),
                "model": .string(SelfEvalApprovalContract.configurationModel),
                "capability": .string(SelfEvalApprovalContract.configurationCapability),
                "result": .string("unavailable"),
                "notes": .string("This runtime has no configured vision provider."),
                "recorded_at": .string("2026-09-09T07:34:00+00:00"),
                "authority": .object(["schema": .string(SelfEvalApprovalContract.visionAttestationSchema)]),
            ])
            let unavailableData = try CanonicalJSON.encode(unavailable)
            try unavailableData.write(to: project.appendingPathComponent(unavailablePath))
            intent["vision_unavailable"] = referenceValue(path: unavailablePath, data: unavailableData)
        }
        let intentValue = JSONValue.object(intent)
        let intentDigest = SelfEvalApprovalRequestVerifier.sha256Hex(try CanonicalJSON.encode(intentValue))
        let attestation = JSONValue.object([
            "schema": .string(isUnavailable ? SelfEvalApprovalContract.visionAttestationSchema : SelfEvalApprovalContract.humanAttestationSchema),
            "attestation_ref": .string(reference),
            "action": .string(action),
            "project_id": .string(projectID),
            "project_root_sha256": .string(SelfEvalApprovalRequestVerifier.sha256Hex(Data(project.path.utf8))),
            "review_intent_sha256": .string(intentDigest),
            "self_eval_result": resultReference,
            "nonce": .string(String(repeating: "a", count: 64)),
            "generation": .integer(1),
            "issued_at": .string("2026-09-09T07:35:00+00:00"),
            "expires_at": .string("2026-09-09T07:40:00+00:00"),
            "key_id": .string(pin.keyID),
            "signature_algorithm": .string(SelfEvalApprovalContract.algorithm),
        ])
        return try CanonicalJSON.encode(.object([
            "schema": .string(SelfEvalApprovalContract.requestSchema),
            "project_root": .string(project.path),
            "action": .string(action),
            "review_input": reviewInput,
            "review_intent": intentValue,
            "review_intent_sha256": .string(intentDigest),
            "attestation": attestation,
        ]))
    }

    private func referenceValue(path: String, data: Data) -> JSONValue {
        .object([
            "path": .string(path),
            "sha256": .string(SelfEvalApprovalRequestVerifier.sha256Hex(data)),
            "bytes": .integer(Int64(data.count)),
        ])
    }
}
