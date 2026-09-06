package eval;

import java.util.ArrayList;
import java.util.List;

/** A greeter: no tree-sitter grammar and no regex patterns, so the generic path runs. */
public class Greeter {
    private static final String DEFAULT_NAME = "world";
    private final List<String> greeted = new ArrayList<>();

    public Greeter() {
    }

    public String greet(String name) {
        String who = name == null ? DEFAULT_NAME : name;
        greeted.add(who);
        return "Hello, " + who + "!";
    }

    public int count() {
        return greeted.size();
    }

    public static void main(String[] args) {
        Greeter g = new Greeter();
        System.out.println(g.greet(args.length > 0 ? args[0] : null));
    }
}
