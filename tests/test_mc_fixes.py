"""Compatibility entry; unittest discovery uses test_base without duplicate cases."""
if __name__ == '__main__':
    import unittest
    from test_base import Fixes
    unittest.main()
