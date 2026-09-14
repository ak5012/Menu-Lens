package assignment1;

/**
 * Minimal test support for Assignment #1. No external testing framework is required.
 */
public final class TestSupport {
    private static int checks = 0;
    private static int failures = 0;

    private TestSupport() {}

    public static void checkEquals(long expected, long actual, String description) {
        checks++;
        if (expected != actual) {
            failures++;
            System.err.println("FAIL: " + description +
                    " (expected " + expected + ", got " + actual + ")");
        }
    }

    public static void checkEquals(String expected, String actual, String description) {
        checks++;
        if (!expected.equals(actual)) {
            failures++;
            System.err.println("FAIL: " + description +
                    " (expected <" + printable(expected) + ">, got <" + printable(actual) + ">)");
        }
    }

    public static void finish() {
        if (failures == 0) {
            System.out.println("PASS: " + checks + " checks");
        } else {
            System.err.println("FAILED: " + failures + " of " + checks + " checks");
            System.exit(1);
        }
    }

    private static String printable(String s) {
        return s.replace("\\", "\\\\")
                .replace("\n", "\\n")
                .replace("\r", "\\r")
                .replace("\t", "\\t");
    }
}
