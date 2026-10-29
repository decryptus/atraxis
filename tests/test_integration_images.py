import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/chain/image-identities.py'
spec = importlib.util.spec_from_file_location('image_identities', SCRIPT)
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


def image_info(entry):
    return {'Id': entry['linux_amd64_image_id'], 'Os': 'linux', 'Architecture': 'amd64',
            'RootFS': {'Layers': entry['rootfs_layers']}, 'Config': {'Env': ['SECRET=must-not-appear']}}


class IntegrationImageTests(unittest.TestCase):
    def test_browser_rejects_drift_and_retains_no_container_environment(self):
        lock = images.load_lock()
        entry = lock['images']['playwright']
        container = {'Image': entry['linux_amd64_image_id'],
                     'Config': {'Image': entry['reference'], 'Env': ['SECRET=must-not-appear']}}
        info = image_info(entry)
        with patch.object(images, 'inspect', side_effect=[container, info]):
            record = images.browser_identity('b' * 64, lock)
        self.assertEqual(record['image_id'], entry['linux_amd64_image_id'])
        self.assertNotIn('SECRET', json.dumps(record))
        for field in ('reference', 'image_id', 'platform', 'layers'):
            candidate, image = copy.deepcopy(container), copy.deepcopy(info)
            if field == 'reference': candidate['Config']['Image'] = 'mutable:tag'
            elif field == 'image_id': candidate['Image'] = 'sha256:' + '0' * 64
            elif field == 'platform': image['Architecture'] = 'arm64'
            else: image['RootFS']['Layers'] = ['sha256:' + '0' * 64]
            with self.subTest(field=field), patch.object(images, 'inspect', side_effect=[candidate, image]):
                with self.assertRaises(ValueError):
                    images.browser_identity('b' * 64, lock)

    def test_chain_requires_all_images_and_their_base_layers(self):
        lock = images.load_lock()
        info = image_info(lock['images']['python'])
        info['RootFS']['Layers'] = info['RootFS']['Layers'] + ['sha256:' + 'e' * 64]
        with patch.object(images, 'inspect', return_value=info) as inspect:
            result = images.chain_identities(lock)
        self.assertEqual(set(result), set(images.CHAIN_IMAGES))
        self.assertEqual(inspect.call_count, 5)
        info['RootFS']['Layers'] = ['sha256:' + '0' * 64]
        with patch.object(images, 'inspect', return_value=info), self.assertRaises(ValueError):
            images.chain_identities(lock)

    def test_source_declarations_cannot_drift_from_lock(self):
        lock = images.load_lock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for p in images.PYTHON_DOCKERFILES + images.DERIVED_DOCKERFILES + images.BROWSER_WORKFLOWS:
                path = root / p
                path.parent.mkdir(parents=True, exist_ok=True)
                if p in images.BROWSER_WORKFLOWS:
                    content = '      image: ' + lock['images']['playwright']['reference']
                else:
                    content = 'FROM ' + (lock['images']['python']['reference']
                                        if p in images.PYTHON_DOCKERFILES else 'atraxis-acceptance:ci')
                path.write_text(content + '\n')
            images.check_declarations(lock, root)
            for p in ('Dockerfile', images.BROWSER_WORKFLOWS[0]):
                path = root / p
                original = path.read_text()
                path.write_text(original.replace('@sha256:', '@changed:'))
                with self.subTest(path=p), self.assertRaises(ValueError):
                    images.check_declarations(lock, root)
                path.write_text(original)

    def test_docker_inspection_is_get_only_bounded_and_does_not_echo_errors(self):
        with patch.object(images, 'DockerConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"Id":"synthetic"}'
            self.assertEqual(images.inspect('images', 'synthetic:tag'), {'Id': 'synthetic'})
            connection.return_value.request.assert_called_once_with('GET', '/images/synthetic%3Atag/json')
            response.read.assert_called_once_with(images.INSPECT_LIMIT + 1)
            for status, body in ((404, b'SECRET=must-not-appear'),
                                 (200, b'x' * (images.INSPECT_LIMIT + 1))):
                response.status, response.read.return_value = status, body
                with self.subTest(status=status), self.assertRaisesRegex(ValueError, '^Docker identity inspection failed$'):
                    images.inspect('images', 'synthetic:tag')
            self.assertEqual(connection.return_value.close.call_count, 3)
