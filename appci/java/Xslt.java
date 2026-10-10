import java.io.File;
import javax.xml.XMLConstants;
import javax.xml.transform.Transformer;
import javax.xml.transform.TransformerFactory;
import javax.xml.transform.stream.StreamResult;
import javax.xml.transform.stream.StreamSource;

/**
 * Render a tool's XML report with its official stylesheet: Xslt <in.xml> <style.xsl> <out.html> [name=value...].
 * No external DTDs or stylesheets are loaded. Java extension functions stay available: Ant's JUnit
 * stylesheet uses one (String.replaceAll), and appci only passes stylesheets it verified against
 * their pinned SHA-256 and extracted privately for this run.
 */
public class Xslt {
    public static void main(String[] args) throws Exception {
        TransformerFactory f = TransformerFactory.newInstance();
        f.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
        f.setAttribute(XMLConstants.ACCESS_EXTERNAL_STYLESHEET, "");
        Transformer t = f.newTransformer(new StreamSource(new File(args[1])));
        for (int i = 3; i < args.length; i++) {
            int eq = args[i].indexOf('=');
            t.setParameter(args[i].substring(0, eq), args[i].substring(eq + 1));
        }
        t.transform(new StreamSource(new File(args[0])), new StreamResult(new File(args[2])));
    }
}
