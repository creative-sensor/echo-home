/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package xml;

import org.xml.sax.Attributes;
import org.xml.sax.SAXException;
import org.xml.sax.helpers.DefaultHandler;

/**
 *
 * @author Creativ
 */
public class TokenXML extends DefaultHandler {

    final String CSID = "csid";
    final String TSN = "tsn";
    final String TSV = "tsv";
    boolean hitCSID = false;
    boolean hitTSN = false;
    boolean hitTSV = false;
    int cs;
    int[] tsn;
    int[] tsv;

    @Override
    public void startElement(String uri, String localName, String qName, Attributes attributes) throws SAXException {
        if (CSID.equalsIgnoreCase(qName)) {
            hitCSID = true;
        }
        if (TSN.equalsIgnoreCase(qName)) {
            hitTSN = true;
        }
        if (TSV.equalsIgnoreCase(qName)) {
            hitTSV = true;
        }
    }

    @Override
    public void characters(char[] ch, int start, int length) throws SAXException {
        String value = new String(ch, start, length);
        if (hitCSID) {
            this.cs = Integer.parseInt(value);
            this.hitCSID = false;
        }
        if (hitTSN) {
            String compositeStr = value.substring(1, value.length() - 1);
            String[] str = compositeStr.split(", ");
            this.tsn = new int[str.length];
            for (int i = 0; i < tsn.length; i++) {
                this.tsn[i] = Integer.parseInt(str[i]);
                //System.out.println("tokenxml tsn: split str["+ i+ "] = " + str[i]);
                //System.out.println("tokenxml tsn: parsed tsn["+ i+ "] = " + tsn[i]);
            }
            this.hitTSN = false;
        }
        if (hitTSV) {
            String compositeStr = value.substring(1, value.length() - 1);
            String[] str = compositeStr.split(", ");
            this.tsv = new int[str.length];
            for (int i = 0; i < tsn.length; i++) {
                this.tsv[i] = Integer.parseInt(str[i]);
                //System.out.println("tokenxml tsv: split str["+ i+ "]" + str[i]);
                //System.out.println("tokenxml tsn: parsed tsv["+ i+ "] = " + tsv[i]);
            }
            this.hitTSV = false;
        }
    }

    public int getCSID() {
        return this.cs;
    }

    public int[] getTSN() {
        return this.tsn;
    }

    public int[] getTSV() {
        return this.tsv;
    }
}
