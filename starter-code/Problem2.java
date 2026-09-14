package assignment1;

/*
 * Starter implementation for Assignment #1, Problem 2.
 *
 * IMPORTANT: This implementation is intentionally imperfect.
 * Your job is NOT to replace it immediately. First, develop a strong
 * test suite that exposes its defects and documents the intended
 * behavior described in the assignment.
 */
public class Problem2 {

    public static String findDollarWords(String s) {
        StringBuilder result = new StringBuilder();

        // Intentionally imperfect implementation.
        String[] words = s.split(" ");
        for (String word : words) {
            int value = 0;

            for (char c : word.toCharArray()) {
                if (c >= 'a' && c <= 'z') {
                    value += c - 'a' + 1;
                } else if (c >= 'A' && c <= 'Z') {
                    value += c - 'A' + 1;
                } else {
                    // Treat punctuation as ending the word.
                    break;
                }
            }

            if (value == 100) {
                result.append(word.toLowerCase()).append(System.lineSeparator());
            }
        }

        return result.toString();
    }

    public static void main(String[] args) {
        java.util.Scanner scanner = new java.util.Scanner(System.in);
        String s = scanner.nextLine();
        System.out.print(findDollarWords(s));
    }
}
