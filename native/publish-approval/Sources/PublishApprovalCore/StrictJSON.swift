import Foundation

public enum JSONValue: Equatable, Sendable {
    case object([String: JSONValue])
    case array([JSONValue])
    case string(String)
    case integer(Int64)
    /// A finite JSON number that is not an Int64. The original token is kept so
    /// canonical verification can reject alternate spellings instead of
    /// rounding at the signing boundary.
    case number(String)
    case bool(Bool)
    case null
}

public enum StrictJSONError: Error, Equatable, CustomStringConvertible {
    case malformed(String)

    public var description: String {
        switch self {
        case let .malformed(message): message
        }
    }
}

/// A small, bounded JSON parser for the approval trust boundary.
///
/// Foundation's JSON decoders accept duplicate object keys. An approval request
/// must not have two interpretations, so this parser rejects duplicate keys,
/// excessive nesting, trailing bytes, invalid UTF-8, and non-integral numbers.
public struct StrictJSONParser: Sendable {
    private var bytes: [UInt8]
    private var index = 0
    private let maximumDepth: Int

    public init(data: Data, maximumDepth: Int = 64) throws {
        guard data.count <= 1_048_576 else {
            throw StrictJSONError.malformed("JSON exceeds the 1 MiB limit")
        }
        guard String(data: data, encoding: .utf8) != nil else {
            throw StrictJSONError.malformed("JSON is not valid UTF-8")
        }
        bytes = Array(data)
        self.maximumDepth = maximumDepth
    }

    public mutating func parse() throws -> JSONValue {
        skipWhitespace()
        let value = try parseValue(depth: 0)
        skipWhitespace()
        guard index == bytes.count else {
            throw error("trailing data")
        }
        return value
    }

    private mutating func parseValue(depth: Int) throws -> JSONValue {
        guard depth <= maximumDepth else {
            throw error("JSON nesting is too deep")
        }
        guard index < bytes.count else {
            throw error("unexpected end of input")
        }
        switch bytes[index] {
        case UInt8(ascii: "{"):
            return try parseObject(depth: depth)
        case UInt8(ascii: "["):
            return try parseArray(depth: depth)
        case UInt8(ascii: "\""):
            return .string(try parseString())
        case UInt8(ascii: "t"):
            try consumeLiteral("true")
            return .bool(true)
        case UInt8(ascii: "f"):
            try consumeLiteral("false")
            return .bool(false)
        case UInt8(ascii: "n"):
            try consumeLiteral("null")
            return .null
        case UInt8(ascii: "-"), UInt8(ascii: "0") ... UInt8(ascii: "9"):
            return try parseNumber()
        default:
            throw error("unexpected token")
        }
    }

    private mutating func parseObject(depth: Int) throws -> JSONValue {
        index += 1
        skipWhitespace()
        var result: [String: JSONValue] = [:]
        if consume(UInt8(ascii: "}")) { return .object(result) }
        while true {
            guard index < bytes.count, bytes[index] == UInt8(ascii: "\"") else {
                throw error("object key must be a string")
            }
            let key = try parseString()
            guard result[key] == nil else {
                throw error("duplicate object key: \(key)")
            }
            skipWhitespace()
            guard consume(UInt8(ascii: ":")) else {
                throw error("missing colon after object key")
            }
            skipWhitespace()
            result[key] = try parseValue(depth: depth + 1)
            skipWhitespace()
            if consume(UInt8(ascii: "}")) { break }
            guard consume(UInt8(ascii: ",")) else {
                throw error("missing comma between object members")
            }
            skipWhitespace()
        }
        return .object(result)
    }

    private mutating func parseArray(depth: Int) throws -> JSONValue {
        index += 1
        skipWhitespace()
        var result: [JSONValue] = []
        if consume(UInt8(ascii: "]")) { return .array(result) }
        while true {
            result.append(try parseValue(depth: depth + 1))
            skipWhitespace()
            if consume(UInt8(ascii: "]")) { break }
            guard consume(UInt8(ascii: ",")) else {
                throw error("missing comma between array elements")
            }
            skipWhitespace()
        }
        return .array(result)
    }

    private mutating func parseString() throws -> String {
        let start = index
        index += 1
        var escaped = false
        while index < bytes.count {
            let byte = bytes[index]
            if byte < 0x20 {
                throw error("unescaped control byte in string")
            }
            index += 1
            if escaped {
                guard byte == UInt8(ascii: "\"")
                    || byte == UInt8(ascii: "\\")
                    || byte == UInt8(ascii: "/")
                    || byte == UInt8(ascii: "b")
                    || byte == UInt8(ascii: "f")
                    || byte == UInt8(ascii: "n")
                    || byte == UInt8(ascii: "r")
                    || byte == UInt8(ascii: "t")
                    || byte == UInt8(ascii: "u")
                else {
                    throw error("invalid string escape")
                }
                if byte == UInt8(ascii: "u") {
                    guard index + 4 <= bytes.count,
                          bytes[index ..< index + 4].allSatisfy(Self.isHex)
                    else {
                        throw error("invalid Unicode escape")
                    }
                    index += 4
                }
                escaped = false
            } else if byte == UInt8(ascii: "\\") {
                escaped = true
            } else if byte == UInt8(ascii: "\"") {
                let token = Data(bytes[start ..< index])
                do {
                    let value = try JSONSerialization.jsonObject(
                        with: token,
                        options: [.fragmentsAllowed]
                    )
                    guard let string = value as? String else {
                        throw error("string could not be decoded")
                    }
                    return string
                } catch {
                    throw self.error("invalid JSON string")
                }
            }
        }
        throw error("unterminated string")
    }

    private mutating func parseNumber() throws -> JSONValue {
        let start = index
        if consume(UInt8(ascii: "-")), index == bytes.count {
            throw error("invalid number")
        }
        if consume(UInt8(ascii: "0")) {
            if index < bytes.count, bytes[index] >= UInt8(ascii: "0"), bytes[index] <= UInt8(ascii: "9") {
                throw error("number has a leading zero")
            }
        } else {
            guard index < bytes.count,
                  bytes[index] >= UInt8(ascii: "1"),
                  bytes[index] <= UInt8(ascii: "9")
            else {
                throw error("invalid number")
            }
            while index < bytes.count,
                  bytes[index] >= UInt8(ascii: "0"),
                  bytes[index] <= UInt8(ascii: "9") {
                index += 1
            }
        }
        var integral = true
        if index < bytes.count, bytes[index] == UInt8(ascii: ".") {
            integral = false
            index += 1
            let fractionStart = index
            while index < bytes.count,
                  bytes[index] >= UInt8(ascii: "0"),
                  bytes[index] <= UInt8(ascii: "9") {
                index += 1
            }
            guard index > fractionStart else { throw error("invalid fraction") }
        }
        if index < bytes.count,
           bytes[index] == UInt8(ascii: "e") || bytes[index] == UInt8(ascii: "E") {
            integral = false
            index += 1
            if index < bytes.count,
               bytes[index] == UInt8(ascii: "+") || bytes[index] == UInt8(ascii: "-") {
                index += 1
            }
            let exponentStart = index
            while index < bytes.count,
                  bytes[index] >= UInt8(ascii: "0"),
                  bytes[index] <= UInt8(ascii: "9") {
                index += 1
            }
            guard index > exponentStart else { throw error("invalid exponent") }
        }
        guard let token = String(bytes: bytes[start ..< index], encoding: .utf8) else {
            throw error("number is invalid UTF-8")
        }
        if integral, let value = Int64(token) { return .integer(value) }
        guard let value = Double(token), value.isFinite else {
            throw error("number is out of range")
        }
        return .number(token)
    }

    private mutating func consumeLiteral(_ literal: StaticString) throws {
        let expected = Array(String(describing: literal).utf8)
        guard index + expected.count <= bytes.count,
              Array(bytes[index ..< index + expected.count]) == expected
        else {
            throw error("invalid literal")
        }
        index += expected.count
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private mutating func skipWhitespace() {
        while index < bytes.count,
              bytes[index] == 0x20 || bytes[index] == 0x09
               || bytes[index] == 0x0A || bytes[index] == 0x0D {
            index += 1
        }
    }

    private func error(_ message: String) -> StrictJSONError {
        .malformed("\(message) at byte \(index)")
    }

    private static func isHex(_ byte: UInt8) -> Bool {
        (byte >= UInt8(ascii: "0") && byte <= UInt8(ascii: "9"))
            || (byte >= UInt8(ascii: "a") && byte <= UInt8(ascii: "f"))
            || (byte >= UInt8(ascii: "A") && byte <= UInt8(ascii: "F"))
    }
}

public enum CanonicalJSON {
    public static func encode(_ value: JSONValue) throws -> Data {
        Data(try encodeString(value).utf8)
    }

    private static func encodeString(_ value: JSONValue) throws -> String {
        switch value {
        case let .object(object):
            return "{" + (try object.keys.sorted().map { key in
                try encodeString(.string(key)) + ":" + encodeString(object[key]!)
            }).joined(separator: ",") + "}"
        case let .array(array):
            return "[" + (try array.map(encodeString)).joined(separator: ",") + "]"
        case let .string(string):
            let data = try JSONSerialization.data(
                withJSONObject: string,
                options: [.fragmentsAllowed, .withoutEscapingSlashes]
            )
            guard let encoded = String(data: data, encoding: .utf8) else {
                throw StrictJSONError.malformed("could not encode UTF-8 string")
            }
            return encoded
        case let .integer(integer):
            return String(integer)
        case let .number(number):
            guard number.range(
                of: #"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$"#,
                options: .regularExpression
            ) != nil,
            let value = Double(number), value.isFinite
            else {
                throw StrictJSONError.malformed("could not encode finite JSON number")
            }
            return number
        case let .bool(boolean):
            return boolean ? "true" : "false"
        case .null:
            return "null"
        }
    }
}

public extension JSONValue {
    var objectValue: [String: JSONValue]? {
        if case let .object(value) = self { value } else { nil }
    }

    var arrayValue: [JSONValue]? {
        if case let .array(value) = self { value } else { nil }
    }

    var stringValue: String? {
        if case let .string(value) = self { value } else { nil }
    }

    var integerValue: Int64? {
        if case let .integer(value) = self { value } else { nil }
    }

    var numberValue: Double? {
        switch self {
        case let .integer(value): Double(value)
        case let .number(value): Double(value)
        default: nil
        }
    }
}
