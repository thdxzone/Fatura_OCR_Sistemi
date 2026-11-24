// macOS Vision ile çevrimdışı OCR. Konumlar sol üst başlangıçlı pikseldir.
import Foundation
import Vision
import AppKit

func recognize(_ image: CGImage, fast: Bool = false) throws -> [[String: Any]] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.usesLanguageCorrection = false
    let supported = try request.supportedRecognitionLanguages()
    request.recognitionLanguages = ["tr-TR", "en-US"].filter { supported.contains($0) }
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    if (request.results ?? []).isEmpty && !fast {
        return try recognize(image, fast: true)
    }
    return (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": candidate.string, "confidence": candidate.confidence,
                "bbox": [box.minX * Double(image.width), (1 - box.maxY) * Double(image.height),
                         box.maxX * Double(image.width), (1 - box.minY) * Double(image.height)]]
    }
}

do {
    guard CommandLine.arguments.count >= 2 else {
        throw NSError(domain: "FaturaOCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "Görsel yolu gerekli"])
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let image = NSImage(contentsOf: url),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        throw NSError(domain: "FaturaOCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "Görsel açılamadı"])
    }
    let width = Double(cgImage.width), height = Double(cgImage.height)
    let supported = try VNRecognizeTextRequest().supportedRecognitionLanguages()
    var output: [String: Any] = ["width": width, "height": height,
        "languages": ["tr-TR", "en-US"].filter { supported.contains($0) }]
    if CommandLine.arguments.count == 3 {
        // Dar bölgeleri ayrı okumak sütunların birbirine karışmasını azaltır.
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2]))
        let regions = try JSONSerialization.jsonObject(with: data) as! [String: [Double]]
        var results: [String: Any] = [:]
        for (name, b) in regions {
            let rect = CGRect(x: b[0] * width, y: b[1] * height,
                              width: (b[2] - b[0]) * width, height: (b[3] - b[1]) * height).integral
            guard let crop = cgImage.cropping(to: rect) else { continue }
            // Küçük tek karakter hücrelerinde tanıma için daha büyük raster kullanılır.
            let scale = 4
            if let context = CGContext(data: nil, width: crop.width * scale, height: crop.height * scale,
                bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) {
                context.interpolationQuality = .high
                context.draw(crop, in: CGRect(x: 0, y: 0, width: crop.width * scale, height: crop.height * scale))
                results[name] = try recognize(context.makeImage() ?? crop)
            } else {
                results[name] = try recognize(crop)
            }
        }
        output["regions"] = results
    } else {
        output["lines"] = try recognize(cgImage)
    }
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys]))
} catch {
    fputs("Yerel OCR başarısız: \(error.localizedDescription)\n", stderr)
    exit(1)
}
