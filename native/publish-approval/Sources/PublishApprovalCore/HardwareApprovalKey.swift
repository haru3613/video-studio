import CryptoKit
import Foundation
import LocalAuthentication
import Security

public protocol ApprovalSigningKey {
    func enroll() throws -> PublicKeyPin
    func keyInfo() throws -> PublicKeyPin
    func sign(_ message: Data, reason: String, expectedKeyID: String) throws -> Data
}

/// The permanent Secure Enclave key can only perform a private-key operation
/// after macOS satisfies userPresence for that exact operation. We deliberately
/// do not treat a prior LAContext policy result as approval.
public final class SecureEnclaveApprovalKey: ApprovalSigningKey {
    public static let applicationTag = "org.videostudio.publish-approval.key.v1"
    private let tag = Data(applicationTag.utf8)

    public init() {}

    public func enroll() throws -> PublicKeyPin {
        switch existingKey(context: nil) {
        case .found:
            throw ApprovalError.key("the approval key already exists; use key-info instead of replacing it")
        case .untrustedToken:
            throw ApprovalError.key("a same-tag non-Secure-Enclave key exists; refusing to replace or trust it")
        case let .unavailable(status) where status != errSecItemNotFound:
            throw ApprovalError.key("could not inspect the approval key (OSStatus \(status))")
        case .unavailable:
            break
        }

        var accessError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            kCFAllocatorDefault,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.userPresence, .privateKeyUsage],
            &accessError
        ) else {
            throw ApprovalError.key("could not create user-presence key policy")
        }
        let attributes: [CFString: Any] = [
            kSecUseDataProtectionKeychain: true,
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits: 256,
            kSecAttrTokenID: kSecAttrTokenIDSecureEnclave,
            kSecPrivateKeyAttrs: [
                kSecAttrIsPermanent: true,
                kSecAttrApplicationTag: tag,
                kSecAttrAccessControl: access,
            ],
        ]
        var keyError: Unmanaged<CFError>?
        guard let privateKey = SecKeyCreateRandomKey(attributes as CFDictionary, &keyError) else {
            let error = keyError?.takeRetainedValue()
            throw ApprovalError.key(
                "Secure Enclave P-256 key enrollment failed (\(Self.safeErrorIdentity(error)))"
            )
        }
        return try pin(for: privateKey)
    }

    public func keyInfo() throws -> PublicKeyPin {
        switch existingKey(context: nil) {
        case let .found(key):
            return try pin(for: key)
        case .untrustedToken:
            throw ApprovalError.key("the tagged approval key is not backed by the Secure Enclave")
        case let .unavailable(status) where status == errSecItemNotFound:
            throw ApprovalError.key("the approval key is not enrolled")
        case let .unavailable(status):
            throw ApprovalError.key("could not load the approval key (OSStatus \(status))")
        }
    }

    public func sign(_ message: Data, reason: String, expectedKeyID: String) throws -> Data {
        let context = LAContext()
        context.touchIDAuthenticationAllowableReuseDuration = 0
        context.localizedReason = reason
        context.localizedCancelTitle = "Do Not Approve"

        let privateKey: SecKey
        switch existingKey(context: context) {
        case let .found(key):
            privateKey = key
        case .untrustedToken:
            throw ApprovalError.key("the tagged approval key is not backed by the Secure Enclave")
        case let .unavailable(status) where status == errSecItemNotFound:
            throw ApprovalError.key("the approval key is not enrolled")
        case let .unavailable(status):
            throw ApprovalError.key("could not load the approval key (OSStatus \(status))")
        }
        let actualPin = try pin(for: privateKey)
        guard actualPin.keyID == expectedKeyID else {
            throw ApprovalError.key("the enrolled key does not match the pinned key_id")
        }
        guard SecKeyIsAlgorithmSupported(privateKey, .sign, .ecdsaSignatureMessageX962SHA256) else {
            throw ApprovalError.key("the enrolled key does not support ECDSA P-256 SHA-256 signing")
        }
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey,
            .ecdsaSignatureMessageX962SHA256,
            message as CFData,
            &error
        ) as Data? else {
            throw ApprovalError.key("user presence was cancelled or signing failed")
        }
        return signature
    }

    private enum KeyLookup {
        case found(SecKey)
        case untrustedToken
        case unavailable(OSStatus)
    }

    private func existingKey(context: LAContext?) -> KeyLookup {
        var query: [CFString: Any] = [
            // Secure Enclave keys live in the data-protection keychain, not
            // the legacy file-based macOS keychain. Query the same store.
            kSecUseDataProtectionKeychain: true,
            kSecClass: kSecClassKey,
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrApplicationTag: tag,
            kSecReturnRef: true,
            kSecReturnAttributes: true,
            kSecMatchLimit: kSecMatchLimitOne,
        ]
        if let context {
            query[kSecUseAuthenticationContext] = context
        } else {
            let noInteraction = LAContext()
            noInteraction.interactionNotAllowed = true
            query[kSecUseAuthenticationContext] = noInteraction
        }
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess else {
            return .unavailable(status)
        }
        guard let dictionary = item as? [CFString: Any],
              let privateKey = dictionary[kSecValueRef] as! SecKey?
        else {
            return .unavailable(errSecInternalError)
        }
        let tokenID = dictionary[kSecAttrTokenID] as? String
        guard tokenID == (kSecAttrTokenIDSecureEnclave as String) else {
            return .untrustedToken
        }
        return .found(privateKey)
    }

    private func pin(for privateKey: SecKey) throws -> PublicKeyPin {
        guard let publicKey = SecKeyCopyPublicKey(privateKey) else {
            throw ApprovalError.key("could not derive the approval public key")
        }
        var error: Unmanaged<CFError>?
        guard let representation = SecKeyCopyExternalRepresentation(publicKey, &error) as Data? else {
            throw ApprovalError.key("could not export the approval public key")
        }
        guard representation.count == 65, representation.first == 0x04 else {
            throw ApprovalError.key("approval public key is not raw uncompressed P-256 X9.63")
        }
        return PublicKeyPin(
            algorithm: ApprovalContract.algorithm,
            keyID: ApprovalRequestVerifier.sha256Hex(representation),
            publicKeyX963: representation
        )
    }

    static func safeErrorIdentity(_ error: CFError?) -> String {
        guard let error else { return "domain=unknown code=unknown" }
        return "domain=\(CFErrorGetDomain(error) as String) code=\(CFErrorGetCode(error))"
    }
}

public enum SignatureVerification {
    public static func verifyP256(message: Data, signature: Data, publicKeyX963: Data) throws -> Bool {
        let attributes: [CFString: Any] = [
            kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeyClass: kSecAttrKeyClassPublic,
            kSecAttrKeySizeInBits: 256,
        ]
        var createError: Unmanaged<CFError>?
        guard let key = SecKeyCreateWithData(publicKeyX963 as CFData, attributes as CFDictionary, &createError) else {
            throw ApprovalError.key("could not reconstruct the approval public key")
        }
        var verifyError: Unmanaged<CFError>?
        return SecKeyVerifySignature(
            key,
            .ecdsaSignatureMessageX962SHA256,
            message as CFData,
            signature as CFData,
            &verifyError
        )
    }
}
