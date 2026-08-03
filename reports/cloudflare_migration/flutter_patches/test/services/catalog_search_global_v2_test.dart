import 'package:card_scanner_app/services/catalog_search_index/catalog_search_index_config.dart';
import 'package:card_scanner_app/services/catalog_search_index/catalog_search_language_registry.dart';
import 'package:card_scanner_app/services/catalog_search_index/catalog_search_manifest.dart';
import 'package:card_scanner_app/services/catalog_search_index/catalog_search_models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('production v1/v2 and worldwide v2.1 schemas remain compatible', () {
    expect(CatalogSearchIndexConfig.supportsSchemaVersion('1.0.0'), isTrue);
    expect(CatalogSearchIndexConfig.supportsSchemaVersion('2.0.0'), isTrue);
    expect(CatalogSearchIndexConfig.supportsSchemaVersion('2.1.0'), isTrue);
    expect(_manifest('2.0.0').isSchemaCompatible(), isTrue);
    expect(_manifest('2.1.0').isSchemaCompatible(), isTrue);
    expect(
      CatalogSearchIndexConfig.supportsCatalogueSchemaVersion('2.2.0'),
      isTrue,
    );
    expect(_manifest('2.1.0', catalogue: '2.2.0').isSchemaCompatible(), isTrue);
    expect(_manifest('2.1.0', catalogue: '9.9.9').isSchemaCompatible(), isFalse);
    expect(
      CatalogSearchIndexConfig.kCloudflareActiveManifestUrl.contains(
        'catalogue.manifest.json',
      ),
      isTrue,
    );
  });

  test('canonical global language and region survive row conversion', () {
    final hit = CatalogSearchHit.fromRow(
      <String, Object?>{
        'canonical_base_id': 'printing-1',
        'language': 'es-419',
        'region': 'LATAM',
        'set_id': 'set-1',
        'collector_number': '001',
        'localized_name': 'Carta',
        'set_name': 'Colección',
      },
      matchClass: CatalogSearchMatchClass.exactName,
      score: 90,
    );
    expect(hit.language, 'es-419');
    expect(hit.region, 'LATAM');
    expect(hit.toCardCandidate().languageCode, 'es-419');
  });

  test('registry keeps simplified and traditional Chinese separate', () {
    expect(CatalogSearchLanguageRegistry.supported, contains('zh-Hans'));
    expect(CatalogSearchLanguageRegistry.supported, contains('zh-Hant'));
    expect(CatalogSearchLanguageRegistry.defaultRegion['zh-Hans'], 'CN');
    expect(CatalogSearchLanguageRegistry.defaultRegion['zh-Hant'], 'MULTI');
  });
}

CatalogSearchManifest _manifest(
  String schema, {
  String catalogue = '1.0.0',
}) => CatalogSearchManifest(
  catalogueSchemaVersion: catalogue,
  searchIndexSchemaVersion: schema,
  generatedAt: '2026-07-11T00:00:00Z',
  generatorVersion: 'test',
  databaseFilename: 'global.sqlite',
  databaseUrl: 'https://example/global.sqlite',
  sha256: List<String>.filled(64, 'a').join(),
  byteSize: 1,
  contentFingerprint: 'f',
  supportedLanguages: const <String>['en', 'ja', 'es-419'],
  totalCardCount: 3,
  perLanguageCounts: const <String, int>{'en': 1, 'ja': 1, 'es-419': 1},
  minimumCompatibleAppVersion: '1.0.0+23',
  minimumCompatibleAppVersionStatus: 'resolved',
  previousDatabaseUrl: null,
  previousSha256: null,
  updatePolicy: 'qa',
  rollbackPolicy: 'delete',
);
