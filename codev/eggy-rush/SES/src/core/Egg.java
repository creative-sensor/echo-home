/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package core;

import java.io.CharArrayReader;
import java.io.IOException;
import java.util.logging.Level;
import java.util.logging.Logger;
import javax.xml.parsers.ParserConfigurationException;
import javax.xml.parsers.SAXParserFactory;
import msg.SESMessage;
import org.xml.sax.InputSource;
import org.xml.sax.SAXException;
import ses.TimeStamp;
import xml.BSSXML;
import xml.NewEggXML;

/**
 *
 * @author Creativ
 */
public class Egg {

    int r; // row
    int c; // column
    int id; // id of the picker
    TimeStamp ts;
    int phase;

    public Egg(int row, int column) {
        this.r = row;
        this.c = column;
        this.id = -1;
        this.ts = null;
        this.phase = 1;
    }

    public int getPickerID() {
        return this.id;
    }

    public TimeStamp getPickTime() {
        return this.ts;
    }

    public int Row() {
        return this.r;
    }

    public int Column() {
        return this.c;
    }

    public void setPickerID(int id) {
        this.id = id;
    }

    public void setPickTime(TimeStamp t) {
        this.ts = t;
    }

    public int Phase() {
        return this.phase;
    }

    public void PhaseUp() {
        this.phase++;
    }

    @Override
    public String toString() {
        String xml = "<newegg>";
        xml += "<row>" + r + "</row>";
        xml += "<col>" + c + "</col>";
        xml += "</newegg>\n";
        return xml;
    }

    public Egg(String data) {
        try {

            SAXParserFactory factory = SAXParserFactory.newInstance();
            javax.xml.parsers.SAXParser saxparser = factory.newSAXParser();
            NewEggXML nexml = new NewEggXML();

            saxparser.parse(new InputSource(new CharArrayReader(data.toCharArray())), nexml);
            this.r = nexml.getParsedRow();
            this.c = nexml.getParsedCol();

        } catch (IOException | ParserConfigurationException ex) {
        } catch (SAXException ex) {
            System.out.print(ex.getMessage());
        }
        
        this.id = -1;
        this.ts = null;
        this.phase = 1;
    }
}
